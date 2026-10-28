"""Preview examples: real mapped pixels and timing adapters, simulated SDK and GUI."""

from contextlib import redirect_stdout
import builtins
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import numpy as np

from bcamsync import Pulse, boot_time_ns
from bcamsync.adapters.libcamera import CameraTiming


ROOT = Path(__file__).resolve().parents[1]


def load_example(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "examples" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


single = load_example("single_board")
dual = load_example("opencv_sync")


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.TemporaryFile()
        self.addCleanup(self.file.close)
        # A nonzero plane offset and padded rows must not become visible pixels.
        self.file.write(b"prefix!" + bytes([1, 2, 3, 4, 99, 99, 99, 99,
                                            5, 6, 7, 8, 99, 99, 99, 99]))
        self.file.flush()
        self.stream = SimpleNamespace(stream=object(), stride=8,
                                      size=SimpleNamespace(width=4, height=2))
        self.buffer = SimpleNamespace(planes=[SimpleNamespace(fd=self.file.fileno(), offset=7)],
                                      metadata=SimpleNamespace(status="success", sequence=17))
        self.request = Mock(cookie=0, status="complete", buffers={self.stream.stream: self.buffer},
                            metadata={"sensor": boot_time_ns(), "duration": 25200})
        self.request.add_buffer.return_value = 0
        self.camera = Mock(id="camera-1")
        self.camera.queue_request.return_value = 0
        self.manager = Mock()
        self.manager.get_ready_requests.return_value = [self.request]
        self.selector = MagicMock()
        self.selector.__enter__.return_value = self.selector
        self.selector.select.return_value = [object()]
        self.sdk = SimpleNamespace(Request=SimpleNamespace(Status=SimpleNamespace(Complete="complete")),
                                   FrameMetadata=SimpleNamespace(Status=SimpleNamespace(Success="success")),
                                   controls=SimpleNamespace(FrameDurationLimits="limits", SensorTimestamp="sensor",
                                                            FrameDuration="duration"))

    def test_list_enumerates_without_importing_image_dependencies_or_opening_cameras(self):
        cameras = [Mock(id="camera-2"), Mock(id="camera-1")]
        manager = SimpleNamespace(cameras=cameras)
        sdk = SimpleNamespace(CameraManager=SimpleNamespace(singleton=lambda: manager))
        for example in (single, dual):
            with self.subTest(example=example.__name__):
                output = io.StringIO()
                with patch.dict(sys.modules, {"libcamera": sdk, "cv2": None, "numpy": None}), \
                        patch.object(sys, "argv", [example.__name__, "--list"]), \
                        redirect_stdout(output):
                    example.main()
                self.assertEqual(output.getvalue().splitlines(), ["camera-1", "camera-2"])
        for camera in cameras:
            camera.acquire.assert_not_called()

    def test_single_board_uses_first_sorted_camera_or_explicit_id(self):
        cameras = [Mock(id="camera-2"), Mock(id="camera-1")]
        manager = Mock(cameras=cameras)
        manager.get.return_value = cameras[0]
        sdk = SimpleNamespace(CameraManager=SimpleNamespace(singleton=lambda: manager))
        for arguments, selected in (([], cameras[1]), (["camera-2"], cameras[0])):
            with self.subTest(arguments=arguments):
                for camera in cameras:
                    camera.reset_mock()
                    camera.acquire.side_effect = RuntimeError("selected camera")
                with patch.dict(sys.modules, {"libcamera": sdk, "cv2": Mock()}), \
                        patch.object(sys, "argv", ["single_board.py", *arguments]), \
                        redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "selected camera"):
                    single.main()
                selected.acquire.assert_called_once()
                for camera in cameras:
                    if camera is not selected:
                        camera.acquire.assert_not_called()

    def test_dual_board_selection_preserves_explicit_reference_order(self):
        manager = SimpleNamespace(cameras=[SimpleNamespace(id=f"camera-{i}") for i in (3, 2, 1)])
        sdk = SimpleNamespace(CameraManager=SimpleNamespace(singleton=lambda: manager))
        for arguments, selected in (([], ["camera-1", "camera-2"]),
                                    (["camera-3", "camera-1"], ["camera-3", "camera-1"])):
            with self.subTest(arguments=arguments):
                output = io.StringIO()
                with patch.dict(sys.modules, {"libcamera": sdk, "cv2": Mock()}), \
                        patch.object(sys, "argv", ["opencv_sync.py", *arguments]), \
                        patch.object(dual, "Capture", side_effect=RuntimeError("selected cameras")) as capture, \
                        redirect_stdout(output), self.assertRaisesRegex(RuntimeError, "selected cameras"):
                    dual.main()
                self.assertEqual(capture.call_args.args[0], selected)
                self.assertIn(f"board-1 (reference): {selected[0]}", output.getvalue())

    def test_dual_loads_image_dependencies_before_constructing_capture(self):
        imported = set()
        original_import = builtins.__import__

        def load(name, *args, **kwargs):
            if name in ("cv2", "numpy"):
                imported.add(name)
            return original_import(name, *args, **kwargs)

        def capture(*_):
            self.assertEqual(imported, {"cv2", "numpy"})
            raise RuntimeError("capture reached")

        with patch.dict(sys.modules, {"cv2": Mock()}), \
                patch("builtins.__import__", side_effect=load), \
                patch.object(sys, "argv", ["opencv_sync.py", "camera-1", "camera-2"]), \
                patch.object(dual, "Capture", side_effect=capture), \
                redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "capture reached"):
            dual.main()

    def test_missing_cameras_fail_before_importing_opencv_or_starting_capture(self):
        for example, camera_ids in ((single, []), (dual, []), (dual, ["camera-1"])):
            with self.subTest(example=example.__name__, camera_ids=camera_ids):
                cameras = [Mock(id=camera_id) for camera_id in camera_ids]
                sdk = SimpleNamespace(CameraManager=SimpleNamespace(
                    singleton=lambda: SimpleNamespace(cameras=cameras)))
                with patch.dict(sys.modules, {"libcamera": sdk, "cv2": None}), \
                        patch.object(sys, "argv", [example.__name__]), \
                        patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
                    example.main()
                self.assertEqual(error.exception.code, 2)
                for camera in cameras:
                    camera.acquire.assert_not_called()

    def test_dual_preview_preserves_sensor_timing_applies_pulse_and_copies_pixels(self):
        timing = CameraTiming("board-1", 40, 20000, 30000)
        timing.bind("preview-test", 1)
        pulse = Pulse()
        pulse.correction_id, pulse.delta_us, pulse.count = 1, 200, 1
        pulse.expires_ns = boot_time_ns() + 1_000_000_000
        timing.adjust(pulse)
        capture = dual.Capture.__new__(dual.Capture)
        capture.sdk, capture.manager = self.sdk, self.manager
        capture.units = {"board-1": timing}
        capture.requests = {0: (self.camera, self.stream, timing, self.request)}
        capture.submitted, capture.images = {}, {}
        capture.error, capture.done, capture.lock = None, threading.Event(), threading.Lock()
        capture.ready, capture.cameras = threading.Event(), []
        capture.queue(self.request)
        self.camera.queue_request.side_effect = lambda _: capture.done.set() or 0
        with patch.object(dual.selectors, "DefaultSelector", return_value=self.selector):
            capture.run()
        self.assertIsNone(capture.error)
        frame = timing.snapshot().frames[0]
        self.assertEqual(frame.sensor_timestamp_ns, self.request.metadata["sensor"])
        self.assertEqual(frame.camera_sequence, 17)
        self.assertEqual(frame.requested_duration_us, 25200)
        self.assertEqual(frame.reported_duration_us, 25200)
        self.assertEqual(frame.correction_id, 1)
        self.assertEqual(self.request.set_control.call_args_list[0].args, ("limits", (25200, 25200)))
        self.assertEqual(self.request.set_control.call_args_list[1].args, ("limits", (25000, 25000)))
        self.assertTrue(capture.submitted[0].restoration)
        np.testing.assert_array_equal(capture.images["board-1"], [[1, 2, 3, 4], [5, 6, 7, 8]])
        # Images own their memory after mmap closes and the SDK reuses its buffer.
        self.file.seek(7)
        self.file.write(bytes(16))
        self.file.flush()
        self.assertEqual(capture.images["board-1"][0, 0], 1)
        self.request.reuse.assert_called_once()

    def test_single_board_previews_without_constructing_a_sync_session(self):
        stream = self.stream
        configuration = Mock()
        configuration.at.return_value = stream
        configuration.validate.return_value = "valid"
        self.camera.generate_configuration.return_value = configuration
        for operation in (self.camera.acquire, self.camera.configure, self.camera.start):
            operation.return_value = 0
        self.camera.create_request.return_value = self.request
        self.manager.get.return_value = self.camera
        allocator = Mock()
        allocator.allocate.return_value = 0
        allocator.buffers.return_value = [self.buffer]
        self.sdk.CameraManager = SimpleNamespace(singleton=lambda: self.manager)
        self.sdk.StreamRole = SimpleNamespace(Viewfinder="preview")
        self.sdk.Size = lambda width, height: stream.size
        self.sdk.PixelFormat = str
        self.sdk.SensorConfiguration = SimpleNamespace
        self.sdk.CameraConfiguration = SimpleNamespace(Status=SimpleNamespace(Invalid="invalid"))
        self.sdk.FrameBufferAllocator = lambda _: allocator
        for control in ("AeEnable", "ExposureTimeMode", "AnalogueGainMode", "ExposureTime", "AnalogueGain"):
            setattr(self.sdk.controls, control, control)
        cv2 = Mock()
        cv2.waitKey.return_value = ord("q")
        with patch.dict(sys.modules, {"cv2": cv2, "libcamera": self.sdk}), \
                patch.object(sys, "argv", ["single_board.py", "camera-1"]), \
                patch.object(single.selectors, "DefaultSelector", return_value=self.selector), \
                patch.object(dual, "SyncSession", side_effect=AssertionError("Single board needs no session")), \
                redirect_stdout(io.StringIO()):
            single.main()
        np.testing.assert_array_equal(cv2.imshow.call_args.args[1], [[1, 2, 3, 4], [5, 6, 7, 8]])
        self.assertEqual(self.camera.queue_request.call_count, 2)
        self.camera.stop.assert_called_once()
        self.camera.release.assert_called_once()
        cv2.destroyAllWindows.assert_called_once()

    def test_sync_exit_keeps_preview_running_and_stop_precedes_capture_close(self):
        for state, fault in (("stopped", ""), ("fault", "peer disconnected")):
            with self.subTest(state=state):
                capture = Mock(units={}, images={"board-1": object()}, lock=threading.Lock(), error=None)
                events = []
                capture.start.side_effect = lambda: events.append("capture-start")
                capture.close.side_effect = lambda: events.append("capture-close")

                def create_capture(camera_ids, config, stack):
                    self.assertEqual(camera_ids, ["camera-1", "camera-2"])
                    self.assertEqual(len(config.units), 2)
                    return capture

                session = Mock()
                session.start.side_effect = lambda: events.append("sync-start")
                session.status.return_value = SimpleNamespace(state=state, ready=False, fault=fault, units=[])
                session.stop.side_effect = lambda: events.append("sync-stop")
                cv2 = Mock()
                cv2.waitKey.side_effect = [-1, -1, ord("q")]
                with patch.dict(sys.modules, {"cv2": cv2}), \
                        patch.object(sys, "argv", ["opencv_sync.py", "camera-1", "camera-2"]), \
                        patch.object(dual, "Capture", side_effect=create_capture), \
                        patch.object(dual, "SyncSession", return_value=session), \
                        redirect_stdout(io.StringIO()):
                    dual.main()
                self.assertEqual(cv2.imshow.call_count, 3)
                self.assertEqual(events, ["capture-start", "sync-start", "sync-stop", "capture-close"])

    def test_config_is_single_node_and_distinct_board_ids_are_required(self):
        config = dual.sync_config()
        self.assertEqual(config.fps, 40)
        self.assertEqual([node.id for node in config.nodes], ["pi5"])
        self.assertEqual([unit.node_id for unit in config.units], ["pi5", "pi5"])
        self.assertEqual(config.reference_unit, "board-1")
        for arguments in (["same", "same"], ["camera-1"], ["camera-1", "camera-2", "camera-3"]):
            with self.subTest(arguments=arguments), \
                    patch.object(sys, "argv", ["opencv_sync.py", *arguments]), \
                    patch.object(dual, "Capture") as capture, \
                    patch.dict(sys.modules, {"libcamera": None}), \
                    patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                dual.main()
            capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
