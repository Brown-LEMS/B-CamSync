"""Exercise cold image imports and first-frame deadlines through the preview entry points."""

import builtins
from contextlib import ExitStack
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripted_libcamera import ScriptedLibcamera
from test_examples import load_example
from test_group_lifecycle import group_config


EXAMPLES = (
    "single_board", "single_board_http",
    "opencv_sync", "opencv_sync_http",
    "two_nodes_sync", "two_nodes_sync_http",
)


class FirstFrameDeadline:
    """The real Capture.start supplies the deadline; simulated imports advance its clock."""

    def __init__(self):
        self.changed = threading.Condition()
        self.now = 0
        self.ready = False
        self.waiting = threading.Event()
        self.expired = threading.Event()

    def wait(self, timeout):
        with self.changed:
            deadline = self.now + timeout
            self.waiting.set()
            if not self.changed.wait_for(lambda: self.ready or self.now >= deadline, timeout=5):
                raise AssertionError("No first frames or simulated deadline")
            if not self.ready:
                self.expired.set()
            return self.ready

    def set(self):
        with self.changed:
            self.ready = True
            self.changed.notify_all()

    def advance(self, seconds):
        with self.changed:
            self.now += seconds
            self.changed.notify_all()


class ImportStartupTests(unittest.TestCase):
    def test_cold_image_imports_finish_before_camera_startup(self):
        for name in EXAMPLES:
            with self.subTest(example=name):
                self.run_preview(name)

    def run_preview(self, name):
        example = load_example(name)
        deadline = FirstFrameDeadline()
        shown = threading.Event()
        quit_preview = threading.Event()
        failures = []
        imported = set()
        original_import = builtins.__import__
        two_nodes = name.startswith("two_nodes")
        synchronized = hasattr(example, "Capture")
        cameras = ("camera-1", "camera-2") if synchronized else ("camera-1",)

        def load(module, globals=None, locals=None, fromlist=(), level=0):
            if module in ("numpy", "cv2") and globals is not None and globals.get("__name__") == name:
                if module not in imported:
                    imported.add(module)
                    in_capture_thread = threading.current_thread() is not app
                    if in_capture_thread:
                        # Hold a cold worker import across the deadline even if the scheduler
                        # starts the worker before Capture.start enters its first-frame wait.
                        self.assertTrue(deadline.waiting.wait(2), "Camera never began first-frame waiting")
                    # Model a first import taking longer than the full startup budget.
                    deadline.advance(1000)
                    if in_capture_thread:
                        self.assertTrue(deadline.expired.wait(2), "First-frame wait did not expire")
                with sdk.changed:
                    sdk.record("import", module)
            return original_import(module, globals, locals, fromlist, level)

        def run():
            try:
                example.main()
            except Exception as error:
                with sdk.changed:
                    failures.append(error)
                    sdk.changed.notify_all()

        def publish(images, _status):
            if images:
                shown.set()
            raise KeyboardInterrupt

        with ExitStack() as stack:
            sdk = ScriptedLibcamera(stack, cameras)
            app = threading.Thread(target=run, name=name, daemon=True)
            cv2 = SimpleNamespace(imshow=lambda *_: shown.set(), destroyAllWindows=lambda: None,
                                  waitKey=lambda _: ord("q") if shown.is_set() or quit_preview.is_set() else -1)
            argv = [name, *cameras]
            stack.enter_context(patch.dict(sys.modules, {"libcamera": sdk.sdk, "cv2": cv2}))
            stack.enter_context(patch.object(sys, "argv", argv))
            selectors = sdk.selectors
            if name == "single_board_http":
                def interruptible_selector():
                    selector = sdk.selector()
                    select = selector.select

                    def select_until_quit(timeout):
                        if quit_preview.is_set():
                            raise KeyboardInterrupt
                        return select(timeout)

                    selector.select = select_until_quit
                    return selector

                selectors = SimpleNamespace(DefaultSelector=interruptible_selector, EVENT_READ=selectors.EVENT_READ)
            stack.enter_context(patch.object(example, "selectors", selectors))
            if two_nodes:
                argv.extend(("--node", "node-b", "--address-a", "127.0.0.1", "--address-b", "127.0.0.2"))
                sync_config = example.sync_config
                endpoints = group_config(2).nodes

                def host_config(*args):
                    config = sync_config(*args)
                    nodes = config.nodes
                    for node, endpoint in zip(nodes, endpoints):
                        node.port = endpoint.port
                    config.nodes = nodes
                    config.clock_mode = "host"
                    return config

                stack.enter_context(patch.multiple(
                    example, sync_config=host_config, discover_boards=lambda *_: (1, 2),
                    shutil=SimpleNamespace(which=lambda command: command)))
            if synchronized:
                capture_type = example.Capture

                def capture(*args):
                    instance = capture_type(*args)
                    instance.ready = deadline
                    return instance

                stack.enter_context(patch.object(example, "Capture", capture))
            if name.endswith("_http"):
                preview = SimpleNamespace(start=lambda: None, close=lambda: None, publish=publish)
                stack.enter_context(patch.object(example, "WebPreview", lambda *_, **_options: preview))
            stack.enter_context(patch.object(builtins, "__import__", load))

            app.start()
            try:
                with sdk.changed:
                    requested = sdk.changed.wait_for(
                        lambda: failures or deadline.expired.is_set() or
                        all(len(camera.pending) == len(camera.buffers) for camera in sdk.cameras.values()),
                        timeout=4)
                if deadline.expired.is_set():
                    app.join(3)
                if failures:
                    raise failures[0]
                self.assertTrue(requested and not deadline.expired.is_set(), list(sdk.events))
                for _ in range(3 if name in ("single_board_http", "opencv_sync_http") else 1):
                    for camera in cameras:
                        sdk.complete(camera)
                self.assertTrue(shown.wait(2), "No frames reached the preview")
            finally:
                quit_preview.set()
                if synchronized and not shown.is_set():
                    deadline.advance(1000)
                app.join(3)
            if failures:
                raise failures[0]
            self.assertFalse(app.is_alive(), "Preview did not shut down")
            startup = [(event, detail) for _, event, detail, *_ in sdk.events
                       if event in ("import", "acquire")]
            self.assertEqual(set(startup[:2]), {("import", "cv2"), ("import", "numpy")}, startup)
            self.assertTrue(all(event == "acquire" for event, _ in startup[2:]), startup)
            self.assertTrue(all(camera.state == "available" for camera in sdk.cameras.values()))
