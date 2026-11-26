"""Run the real two-node preview lifecycle against scripted camera completions."""

from contextlib import ExitStack, contextmanager
import importlib.util
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bcamsync import SyncSession
from bcamsync.adapters.libcamera import CameraTiming

from scripted_libcamera import ScriptedLibcamera
from synthetic_capture import SyntheticCapture
from test_group_lifecycle import group_config


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "two_nodes_sync.py"
CAMERAS = ("camera-1", "camera-2")
BASELINE_US = 25_000


class ObservedTiming(CameraTiming):
    def __init__(self, *args):
        super().__init__(*args)
        self.cancelled = threading.Event()

    def cancel(self):
        super().cancel()
        self.cancelled.set()


class CameraSimulationTests(unittest.TestCase):
    @contextmanager
    def preview(self, stack, *, multi_node):
        # Keep NumPy loaded when the temporary SDK and GUI modules are removed.
        importlib.import_module("numpy")
        spec = importlib.util.spec_from_file_location("simulated_two_nodes", EXAMPLE)
        example = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(example)
        config = example.sync_config("127.0.0.1", "127.0.0.2", boards_a=1, boards_b=2)
        loopback = group_config(2)
        nodes = config.nodes
        for node, endpoint in zip(nodes, loopback.nodes):
            node.address, node.port = endpoint.address, endpoint.port
        config.nodes = nodes
        if not multi_node:
            config.nodes = [nodes[1]]
            config.units = [unit for unit in config.units if unit.node_id == "node-b"]
            config.reference_unit = "board-b1"
        config.clock_mode = "host"
        config.phase_policy = loopback.phase_policy
        config.startup_timeout_seconds = 6
        config.request_timeout_seconds = 2
        config.validate()

        sdk = ScriptedLibcamera(stack, CAMERAS)
        quit_preview = threading.Event()
        previewing = threading.Event()
        failures = []
        follower = SimpleNamespace(capture=None, session=None, units=None)
        capture_class = example.Capture

        def wait_key(milliseconds):
            previewing.set()
            return ord("q") if quit_preview.wait(milliseconds / 1000) else -1

        def create_session(config, node, units):
            follower.session = SyncSession(config, node, units)
            follower.units = units
            with sdk.changed:
                sdk.record("create session", node)
            return follower.session

        def create_capture(*args):
            follower.capture = capture_class(*args)
            return follower.capture

        def run():
            try:
                example.main()
            except Exception as error:
                failures.append(error)

        argv = [str(EXAMPLE), "--node", "node-b", "--address-a", "127.0.0.1",
                "--address-b", "127.0.0.2", *CAMERAS]
        cv2 = SimpleNamespace(waitKey=wait_key, imshow=lambda *_: None, destroyAllWindows=lambda: None)
        worker = threading.Thread(target=run, name="simulated-preview", daemon=True)
        with ExitStack() as application:
            application.enter_context(patch.dict(sys.modules, {"libcamera": sdk.sdk, "cv2": cv2}))
            application.enter_context(patch.object(sys, "argv", argv))
            application.enter_context(patch.multiple(
                example, sync_config=lambda *_: config, discover_boards=lambda *_: (1, 2),
                shutil=SimpleNamespace(which=lambda command: command), selectors=sdk.selectors,
                CameraTiming=ObservedTiming, Capture=create_capture, SyncSession=create_session))
            worker.start()
            try:
                sdk.wait_for_requests()
                yield sdk, config, follower, previewing, quit_preview
                quit_preview.set()
                worker.join(5)
                self.assertFalse(worker.is_alive(), "Preview did not shut down")
                if failures:
                    raise failures[0]
            except Exception:
                with sdk.changed:
                    for event in sdk.events:
                        print(*event, file=sys.stderr)
                raise
            finally:
                quit_preview.set()
                worker.join(10)

    def test_sync_starts_only_after_both_local_boards_deliver_a_frame(self):
        with ExitStack() as stack, self.preview(stack, multi_node=False) as (sdk, _, follower, previewing, _quit):
            sdk.complete(CAMERAS[0])
            self.assertFalse(follower.capture.ready.is_set(), "One local board cannot finish camera startup")
            self.assertIsNone(follower.session, "One local board cannot finish camera startup")
            sdk.complete(CAMERAS[1])
            self.assertTrue(previewing.wait(3), "Preview did not start after both first frames")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if follower.session.status().ready:
                    return
                for camera_id in CAMERAS:
                    sdk.complete(camera_id)
            self.fail(f"Local synchronization did not become ready: {follower.session.status().fault}")

    def test_follower_restores_period_before_closing_cameras(self):
        with ExitStack() as stack, self.preview(stack, multi_node=True) as (sdk, config, follower, previewing, quit_preview):
            reference = SyntheticCapture(config.reference_unit)
            reference.start_capture()
            stack.callback(reference.stop_capture)
            coordinator = SyncSession(config, "node-a", {reference.unit_id: reference})
            stack.callback(coordinator.stop)
            coordinator.start()

            sdk.complete(CAMERAS[0], phase_ns=6_000_000)
            sdk.complete(CAMERAS[1])
            self.assertTrue(previewing.wait(3), "Preview did not start after both first frames")
            timing = follower.units["board-b1"]

            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                sdk.complete(CAMERAS[0])
                with sdk.changed:
                    camera = sdk.cameras[CAMERAS[0]]
                    durations = [request.controls["FrameDurationLimits"][0] for request in camera.pending]
                if any(duration != BASELINE_US for duration in durations):
                    break
                sdk.complete(CAMERAS[1])
            else:
                self.fail(f"Coordinator did not submit a correction: {follower.session.status().fault}")

            with sdk.changed:
                sdk.record("request stop", "node-b")
            quit_preview.set()
            self.assertTrue(timing.cancelled.wait(2), "Stopping the follower did not cancel the live correction")
            correction_id = timing.snapshot().correction.correction_id
            with sdk.changed:
                sdk.record("cancel correction", timing.unit_id, correction_id)

            # Drain the older request, then the corrected request, then the restoration
            # request. That last completion still reports the earlier corrected period.
            self.assertEqual(sdk.complete(CAMERAS[0]), BASELINE_US)
            self.assertEqual(sdk.complete(CAMERAS[0]), BASELINE_US)
            self.assertNotEqual(sdk.complete(CAMERAS[0]), BASELINE_US)
            self.assertEqual(timing.snapshot().correction.state, "settling")
            self.assertEqual(camera.state, "running")

            self.assertEqual(sdk.complete(CAMERAS[0]), BASELINE_US)
            restored = timing.snapshot().correction
            self.assertEqual((restored.state, restored.correction_id), ("restored", correction_id))
            # Cold calibration also pulses the other local board. Capture keeps
            # delivering its pending requests until that board returns to baseline.
            other = follower.units["board-b2"]
            while other.snapshot().correction.state in {"submitting", "settling"}:
                sdk.complete(CAMERAS[1])

        for session in (follower.session, coordinator):
            status = session.status()
            self.assertEqual(status.fault, "")
            self.assertEqual((status.state, status.stop_node, status.stop_reason),
                             ("stopped", "node-b", "requested"))
        self.assertTrue(all(camera.state == "available" and not camera.pending
                            for camera in sdk.cameras.values()))
