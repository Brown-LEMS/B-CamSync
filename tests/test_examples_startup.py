"""Camera startup ordering and cleanup with simulated SDK and timing ports."""

from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import numpy as np


EXAMPLES = ("opencv_sync", "opencv_sync_http", "two_nodes_sync", "two_nodes_sync_http")
ROOT = Path(__file__).resolve().parents[1]


def load_example(name):
    # These application lifecycle tests do not exercise the native sync engine.
    core, adapter = ModuleType("bcamsync"), ModuleType("bcamsync.adapters.libcamera")
    for symbol in ("NodeConfig", "SyncConfig", "SyncSession", "SyncUnitConfig"):
        setattr(core, symbol, Mock())
    adapter.CameraTiming = lambda unit, fps, low, high: Mock(
        unit_id=unit, baseline_us=25000,
        next_submission=Mock(return_value=SimpleNamespace(duration_us=25000)))
    spec = importlib.util.spec_from_file_location(name, ROOT / "examples" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"bcamsync": core, "bcamsync.adapters.libcamera": adapter}):
        spec.loader.exec_module(module)
    return module


def completed(requests):
    for request in requests:
        buffer = next(iter(request.buffers.values()))
        buffer.metadata.sequence += 1
        request.metadata["SensorTimestamp"] = time.monotonic_ns()
    return requests


class StartupTests(unittest.TestCase):
    def fixture(self, example, stack):
        events, cameras, requests = [], [], []
        storage = stack.enter_context(tempfile.TemporaryFile())
        storage.write(bytes(16))
        storage.flush()
        for index in (1, 2):
            camera = Mock(id=f"camera-{index}")
            cameras.append(camera)
            stream = SimpleNamespace(stream=object(), stride=8,
                                     size=SimpleNamespace(width=4, height=2))
            buffer = SimpleNamespace(planes=[SimpleNamespace(fd=storage.fileno(), offset=0)],
                                     metadata=SimpleNamespace(status="success", sequence=1))
            requests.append(None)

            def create(cookie, i=index, buffer=buffer, stream=stream):
                events.append(("request", i))
                request = Mock(cookie=cookie, status="complete", buffers={stream.stream: buffer},
                               metadata={"SensorTimestamp": 0, "FrameDuration": 25000})
                request.add_buffer.return_value = 0
                requests[i - 1] = request
                return request

            camera.acquire.return_value = 0
            camera.configure.side_effect = lambda _, i=index: events.append(("configure", i)) or 0
            camera.create_request.side_effect = create
            camera.start.side_effect = lambda _, i=index: events.append(("start", i)) or 0
            camera.stop.side_effect = lambda i=index: events.append(("stop", i))
            camera.release.side_effect = lambda i=index: events.append(("release", i))
            camera.queue_request.return_value = 0
            camera.controls = {"FrameDurationLimits": SimpleNamespace(min=10000, max=100000)}
            configuration = camera.generate_configuration.return_value
            configuration.validate.return_value = "valid"
            configuration.at.return_value = stream
            camera.allocator = Mock(allocate=Mock(return_value=0), buffers=Mock(return_value=[buffer]))
        manager = Mock()
        manager.get.side_effect = dict((camera.id, camera) for camera in cameras).get
        manager.get_ready_requests.side_effect = lambda: completed(requests)
        sdk = SimpleNamespace(
            CameraManager=SimpleNamespace(singleton=lambda: manager),
            StreamRole=SimpleNamespace(Viewfinder="preview"), Size=lambda *_: SimpleNamespace(width=4, height=2),
            PixelFormat=str, SensorConfiguration=SimpleNamespace,
            CameraConfiguration=SimpleNamespace(Status=SimpleNamespace(Invalid="invalid")),
            FrameBufferAllocator=lambda camera: camera.allocator,
            Request=SimpleNamespace(Status=SimpleNamespace(Complete="complete")),
            FrameMetadata=SimpleNamespace(Status=SimpleNamespace(Success="success")),
            controls=SimpleNamespace(**{name: name for name in (
                "FrameDurationLimits", "AeEnable", "ExposureTimeMode", "AnalogueGainMode",
                "ExposureTime", "AnalogueGain", "SensorTimestamp", "FrameDuration")}))
        selector = MagicMock()
        selector.__enter__.return_value = selector
        selector.select.side_effect = lambda _: threading.Event().wait(0.001) or [object()]
        stack.enter_context(patch.dict(sys.modules, {"libcamera": sdk}))
        stack.enter_context(patch.object(example.selectors, "DefaultSelector", return_value=selector))
        units = [SimpleNamespace(id=f"board-{i}", node_id="local") for i in (1, 2)]
        arguments = ([camera.id for camera in cameras], SimpleNamespace(units=units, fps=40))
        if example.__name__.startswith("two_nodes"):
            arguments += ("local",)
        capture = example.Capture(*arguments, stack)
        stack.callback(capture.close)
        return capture, cameras, requests, manager, events

    def test_prepare_all_requests_before_start_and_stop_before_release(self):
        for name in EXAMPLES:
            with self.subTest(example=name), ExitStack() as stack:
                capture, cameras, requests, manager, events = self.fixture(load_example(name), stack)
                self.assertEqual(events, [("configure", 1), ("request", 1), ("configure", 2), ("request", 2)])
                capture.start(np)
                self.assertIsNone(capture.error)
                self.assertEqual(set(capture.images), {"board-1", "board-2"})
                self.assertTrue(capture.ready.is_set())
                stack.close()
                self.assertEqual(events[-4:], [("stop", 2), ("stop", 1), ("release", 2), ("release", 1)])

    def test_partial_start_failure_propagates_and_stops_only_started_cameras(self):
        for name in EXAMPLES:
            with self.subTest(example=name), ExitStack() as stack:
                capture, cameras, _, _, _ = self.fixture(load_example(name), stack)
                cameras[1].start.side_effect = RuntimeError("second camera failed")
                with self.assertRaisesRegex(RuntimeError, "second camera failed"):
                    capture.start(np)
                stack.close()
                cameras[0].stop.assert_called_once()
                cameras[1].stop.assert_not_called()
                for camera in cameras:
                    camera.release.assert_called_once()

    def test_cancelled_first_request_identifies_board_and_aborts_start(self):
        for name in ("opencv_sync", "two_nodes_sync", "two_nodes_sync_http"):
            with self.subTest(example=name), ExitStack() as stack:
                capture, _, requests, _, _ = self.fixture(load_example(name), stack)
                requests[0].status = "cancelled"
                with self.assertRaisesRegex(RuntimeError, r"cancelled: board-1 \(camera-1\)"):
                    capture.start(np)
                self.assertFalse(capture.images)

    def test_first_frame_from_one_board_does_not_finish_startup(self):
        for name in EXAMPLES:
            with self.subTest(example=name), ExitStack() as stack:
                capture, _, requests, manager, _ = self.fixture(load_example(name), stack)
                second_frame = threading.Event()
                manager.get_ready_requests.side_effect = lambda: completed(requests if second_frame.is_set() else requests[:1])
                capture.thread = threading.Thread(target=capture.run, args=(np,))
                capture.thread.start()
                self.assertFalse(capture.ready.wait(0.03))
                second_frame.set()
                self.assertTrue(capture.ready.wait(1))
                self.assertIsNone(capture.error)

    def test_http_retries_only_failed_board_and_discards_old_cancellations(self):
        example = load_example("opencv_sync_http")
        with ExitStack() as stack:
            capture, cameras, requests, manager, _ = self.fixture(example, stack)
            old = requests[0]
            old.status = "cancelled"
            cancel_after_ready = threading.Event()

            def ready():
                if cancel_after_ready.is_set():
                    requests[1].status = "cancelled"
                current = completed(requests)
                return [old, *current] if current[0] is not old else current

            manager.get_ready_requests.side_effect = ready
            capture.start(np)
            self.assertEqual([camera.start.call_count for camera in cameras], [2, 1])
            self.assertEqual(cameras[0].stop.call_count, 1)
            self.assertNotEqual(requests[0].cookie, old.cookie)
            self.assertGreaterEqual(cameras[0].queue_request.call_count, 4)
            self.assertTrue(all(unit.observe.called for unit in capture.units.values()))
            cancel_after_ready.set()
            capture.thread.join(2)
            self.assertRegex(str(capture.error), "cancelled: board-2")
            self.assertEqual([camera.start.call_count for camera in cameras], [2, 1])

    def test_http_persistent_cancellations_exhaust_exactly_twenty_retries(self):
        example = load_example("opencv_sync_http")
        with ExitStack() as stack:
            capture, cameras, requests, manager, _ = self.fixture(example, stack)

            def cancelled():
                requests[0].status = "cancelled"
                return completed(requests)

            manager.get_ready_requests.side_effect = cancelled
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaisesRegex(RuntimeError, "startup exhausted: board-1"):
                capture.start(np)
            records = [json.loads(line.removeprefix("B-CamSync startup ")) for line in output.getvalue().splitlines()]
            self.assertEqual([record["attempt"] for record in records], list(range(1, 22)))
            self.assertEqual([record["outcome"] for record in records], ["retry"] * 20 + ["exhausted"])
            self.assertTrue(all(record["valid_frames"] == 0 for record in records))
            self.assertEqual([camera.start.call_count for camera in cameras], [21, 1])
            self.assertFalse(capture.units["board-1"].observe.called)

    def test_http_no_output_restarts_camera_even_without_cancel_notification(self):
        example = load_example("opencv_sync_http")
        with ExitStack() as stack:
            capture, cameras, requests, manager, _ = self.fixture(example, stack)
            # Deliver data on the healthy board throughout the first board's initial stall.
            manager.get_ready_requests.side_effect = lambda: completed(
                requests if cameras[0].start.call_count > 1 else requests[1:])
            with patch.object(example, "ATTEMPT_SECONDS", 0.04):
                capture.start(np)
            self.assertEqual([camera.start.call_count for camera in cameras], [2, 1])
            self.assertIsNone(capture.error)

    def test_http_revalidates_a_previously_qualified_board_that_stalls_while_waiting(self):
        example = load_example("opencv_sync_http")
        with ExitStack() as stack:
            capture, cameras, requests, manager, _ = self.fixture(example, stack)
            ticks = 0

            def alternating():
                nonlocal ticks
                ticks += 1
                if ticks <= 3:
                    batch = requests[:1]
                elif cameras[0].start.call_count == 1:
                    batch = requests[1:]
                else:
                    batch = requests
                for request in completed(batch):
                    request.metadata["SensorTimestamp"] = ticks * 25_000_000
                return batch

            manager.get_ready_requests.side_effect = alternating
            with patch.object(example, "ATTEMPT_SECONDS", 0.06):
                capture.start(np)
            self.assertEqual([camera.start.call_count for camera in cameras], [2, 1])
            self.assertTrue(capture.units["board-1"].observe.called)


if __name__ == "__main__":
    unittest.main()
