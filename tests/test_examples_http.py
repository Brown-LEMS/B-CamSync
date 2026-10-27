"""HTTP examples: real JPEG transport and cleanup, simulated camera SDK and encoder."""

from contextlib import redirect_stdout
import builtins
from http.client import HTTPConnection
from io import BytesIO
import io
import json
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import numpy as np
from PIL import Image

from test_examples import load_example


single = load_example("single_board_http")
dual = load_example("opencv_sync_http")


def encoder():
    def encode(_format, pixels, _options):
        output = BytesIO()
        Image.fromarray(pixels).save(output, format="JPEG")
        return True, np.frombuffer(output.getvalue(), dtype=np.uint8)
    return Mock(imencode=Mock(side_effect=encode))


def request(preview, path):
    connection = HTTPConnection("127.0.0.1", preview.server.server_port, timeout=2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


class HttpPreviewTests(unittest.TestCase):
    def test_real_http_page_jpeg_status_and_browser_reconnect(self):
        for example, boards in ((single, ("board-1",)), (dual, ("board-1", "board-2"))):
            with self.subTest(example=example.__name__):
                preview = example.WebPreview("127.0.0.1", 0, boards)
                try:
                    with redirect_stdout(io.StringIO()):
                        preview.start()
                    code, headers, page = request(preview, "/")
                    self.assertEqual(code, 200)
                    self.assertIn("text/html", headers["Content-Type"])
                    for board in boards:
                        self.assertIn(f'data-src="/{board}.jpg"'.encode(), page)
                        self.assertEqual(request(preview, f"/{board}.jpg")[0], 204)
                    self.assertEqual(request(preview, "/unknown.jpg")[0], 404)
                    self.assertEqual(json.loads(request(preview, "/status")[2]), {"state": "starting"})
                    images = {board: np.full((8, 32), 80 + index * 40, dtype=np.uint8)
                              for index, board in enumerate(boards)}
                    status = {"state": "capturing"} if example is single else {
                        "state": "tracking", "ready": True, "fault": "", "units": []}
                    cv2 = encoder()
                    with patch.dict(sys.modules, {"cv2": cv2}):
                        preview.publish(images, status)
                        for index, board in enumerate(boards):
                            code, headers, jpeg = request(preview, f"/{board}.jpg?refresh=1")
                            self.assertEqual((code, headers["Content-Type"]), (200, "image/jpeg"))
                            self.assertEqual(headers["Cache-Control"], "no-store")
                            with Image.open(BytesIO(jpeg)) as image:
                                self.assertEqual(image.size, (32, 8))
                                self.assertEqual(image.getpixel((0, 0)), 80 + index * 40)
                        self.assertEqual(json.loads(request(preview, "/status")[2]), status)
                        # Every request closes its connection; reconnecting sees a new publication.
                        preview.publish({board: image + 10 for board, image in images.items()}, status)
                        with Image.open(BytesIO(request(preview, f"/{boards[0]}.jpg")[2])) as image:
                            self.assertEqual(image.getpixel((0, 0)), 90)
                    cv2.imshow.assert_not_called()
                    cv2.waitKey.assert_not_called()
                finally:
                    preview.close()
                self.assertFalse(preview.thread.is_alive())
                self.assertEqual(preview.server.socket.fileno(), -1)

    def test_jpeg_failure_is_reported_and_unstarted_server_can_close(self):
        for example in (single, dual):
            with self.subTest(example=example.__name__):
                preview = example.WebPreview("127.0.0.1", 0, ("board-1",))
                try:
                    cv2 = Mock()
                    cv2.imencode.return_value = False, None
                    with patch.dict(sys.modules, {"cv2": cv2}), \
                            self.assertRaisesRegex(RuntimeError, "JPEG encoding failed"):
                        preview.publish({"board-1": np.zeros((2, 2), dtype=np.uint8)}, {})
                finally:
                    preview.close()
                self.assertEqual(preview.server.socket.fileno(), -1)

    def test_list_and_insufficient_devices_need_no_encoder_or_http_server(self):
        cameras = [Mock(id="camera-2"), Mock(id="camera-1")]
        manager = SimpleNamespace(cameras=cameras)
        sdk = SimpleNamespace(CameraManager=SimpleNamespace(singleton=lambda: manager))
        for example in (single, dual):
            with self.subTest(example=example.__name__):
                output = io.StringIO()
                with patch.dict(sys.modules, {"libcamera": sdk, "cv2": None, "numpy": None}), \
                        patch.object(sys, "argv", [example.__name__, "--list"]), \
                        patch.object(example, "WebPreview") as server, redirect_stdout(output):
                    example.main()
                self.assertEqual(output.getvalue().splitlines(), ["camera-1", "camera-2"])
                server.assert_not_called()
                manager.cameras = []
                with patch.dict(sys.modules, {"libcamera": sdk, "cv2": None}), \
                        patch.object(sys, "argv", [example.__name__]), \
                        patch.object(example, "WebPreview") as server, \
                        patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                    example.main()
                server.assert_not_called()
                manager.cameras = cameras
        for camera in cameras:
            camera.acquire.assert_not_called()

    def test_dual_sync_fault_keeps_http_capture_alive_and_ctrl_c_cleans_up(self):
        capture = Mock(units={}, images={"board-1": np.full((2, 4), 128, dtype=np.uint8)},
                       lock=threading.Lock(), error=None)
        session = Mock()
        session.status.return_value = SimpleNamespace(state="fault", ready=False, fault="clock fault", units=[])
        events, previews = [], []
        preview_class = dual.WebPreview
        capture.start.side_effect = lambda: events.append("capture-start")
        capture.close.side_effect = lambda: events.append("capture-close")
        session.stop.side_effect = lambda: events.append("sync-stop")

        def create_preview(_address, _port, boards):
            preview = preview_class("127.0.0.1", 0, boards)
            previews.append(preview)
            return preview

        iterations = []

        def pause(_seconds):
            status = json.loads(request(previews[0], "/status")[2])
            self.assertFalse(status["ready"])
            self.assertEqual(status["fault"], "clock fault")
            self.assertEqual(request(previews[0], "/board-1.jpg")[0], 200)
            capture.close.assert_not_called()
            session.stop.assert_not_called()
            iterations.append(1)
            if len(iterations) == 2:
                raise KeyboardInterrupt

        cv2 = encoder()
        with patch.dict(sys.modules, {"cv2": cv2}), \
                patch.object(sys, "argv", ["opencv_sync_http.py", "camera-1", "camera-2"]), \
                patch.object(dual, "Capture", return_value=capture), \
                patch.object(dual, "SyncSession", return_value=session), \
                patch.object(dual, "WebPreview", side_effect=create_preview), \
                patch.object(dual.time, "sleep", side_effect=pause), redirect_stdout(io.StringIO()):
            dual.main()
        self.assertEqual(len(iterations), 2)
        self.assertEqual(events, ["capture-start", "sync-stop", "capture-close"])
        self.assertFalse(previews[0].thread.is_alive())
        self.assertEqual(previews[0].server.socket.fileno(), -1)
        cv2.imshow.assert_not_called()
        cv2.waitKey.assert_not_called()

    def test_image_dependencies_load_before_acquiring_or_starting_cameras(self):
        camera = Mock(id="camera-1")
        manager = Mock()
        manager.get.return_value = camera
        sdk = SimpleNamespace(CameraManager=SimpleNamespace(singleton=lambda: manager))
        original_import = builtins.__import__
        for example, ids in ((single, ["camera-1"]), (dual, ["camera-1", "camera-2"])):
            with self.subTest(example=example.__name__):
                imported = set()

                def load(name, *args, **kwargs):
                    if name in ("cv2", "numpy"):
                        imported.add(name)
                    return original_import(name, *args, **kwargs)

                def capture(*_):
                    self.assertEqual(imported, {"cv2", "numpy"})
                    raise RuntimeError("capture reached")

                camera.acquire.side_effect = capture
                with patch.dict(sys.modules, {"libcamera": sdk, "cv2": Mock()}), \
                        patch("builtins.__import__", side_effect=load), \
                        patch.object(sys, "argv", [example.__name__, *ids]), \
                        patch.object(example, "WebPreview"), \
                        patch.object(dual, "Capture", side_effect=capture), \
                        redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "capture reached"):
                    example.main()

    def test_single_camera_keeps_requeueing_and_stops_without_gui_calls(self):
        with tempfile.TemporaryFile() as storage:
            storage.write(bytes(range(16)))
            storage.flush()
            stream = SimpleNamespace(stream=object(), stride=8, size=SimpleNamespace(width=4, height=2))
            buffer = SimpleNamespace(planes=[SimpleNamespace(fd=storage.fileno(), offset=0)],
                                     metadata=SimpleNamespace(status="success"))
            camera_request = Mock(status="complete", buffers={stream.stream: buffer})
            camera_request.add_buffer.return_value = 0
            camera = Mock(id="camera-1")
            for operation in (camera.acquire, camera.configure, camera.start, camera.queue_request):
                operation.return_value = 0
            camera.create_request.return_value = camera_request
            configuration = camera.generate_configuration.return_value
            configuration.at.return_value = stream
            configuration.validate.return_value = "valid"
            allocator = Mock()
            allocator.allocate.return_value = 0
            allocator.buffers.return_value = [buffer]
            manager = Mock(cameras=[camera])
            manager.get_ready_requests.return_value = [camera_request]
            sdk = SimpleNamespace(CameraManager=SimpleNamespace(singleton=lambda: manager),
                                  StreamRole=SimpleNamespace(Viewfinder="preview"),
                                  Size=lambda *_: stream.size, PixelFormat=str, SensorConfiguration=SimpleNamespace,
                                  CameraConfiguration=SimpleNamespace(Status=SimpleNamespace(Invalid="invalid")),
                                  FrameBufferAllocator=lambda _: allocator,
                                  Request=SimpleNamespace(Status=SimpleNamespace(Complete="complete")),
                                  FrameMetadata=SimpleNamespace(Status=SimpleNamespace(Success="success")),
                                  controls=SimpleNamespace(**{name: name for name in (
                                      "FrameDurationLimits", "AeEnable", "ExposureTimeMode", "AnalogueGainMode",
                                      "ExposureTime", "AnalogueGain")}))
            selector = MagicMock()
            selector.__enter__.return_value = selector
            selector.select.side_effect = [[object()], KeyboardInterrupt]
            preview = Mock()
            cv2 = Mock()
            with patch.dict(sys.modules, {"libcamera": sdk, "cv2": cv2}), \
                    patch.object(sys, "argv", ["single_board_http.py"]), \
                    patch.object(single, "WebPreview", return_value=preview), \
                    patch.object(single.selectors, "DefaultSelector", return_value=selector), \
                    redirect_stdout(io.StringIO()):
                single.main()
            np.testing.assert_array_equal(preview.publish.call_args.args[0]["board-1"],
                                          [[0, 1, 2, 3], [8, 9, 10, 11]])
            self.assertEqual(camera.queue_request.call_count, 2)
            camera_request.reuse.assert_called_once()
            camera.stop.assert_called_once()
            camera.release.assert_called_once()
            preview.close.assert_called_once()
            cv2.imshow.assert_not_called()
            cv2.waitKey.assert_not_called()


if __name__ == "__main__":
    unittest.main()
