"""External example preserves sensor timing and observes baseline restoration."""

from http.client import HTTPConnection
from io import BytesIO
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

from bcamsync import Pulse, boot_time_ns

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from opencv_sync import BrowserPreview, CameraTiming, main


class CameraTimingTests(unittest.TestCase):
    def setUp(self):
        self.unit = CameraTiming("board", 40, 20000, 30000)
        self.unit.bind("camera-test", 1)
        pulse = Pulse()
        pulse.correction_id, pulse.delta_us, pulse.count = 1, 200, 2
        pulse.expires_ns = boot_time_ns() + 1_000_000_000
        self.pulse = pulse

    def test_restore_requires_observed_baseline_and_preserves_sensor_timestamp(self):
        self.unit.adjust(self.pulse)
        first, second, restore = [self.unit.next_submission() for _ in range(3)]
        self.assertEqual([first.duration_us, second.duration_us, restore.duration_us],
                         [25200, 25200, 25000])
        self.assertTrue(restore.restoration)
        sensor_ns = boot_time_ns()
        self.unit.observe(10, sensor_ns, 25200, restore)
        snapshot = self.unit.snapshot()
        self.assertEqual(snapshot.correction.state, "settling")
        self.assertEqual(snapshot.frames[-1].sensor_timestamp_ns, sensor_ns)
        self.assertEqual(snapshot.frames[-1].timestamp_ns, sensor_ns)
        self.assertEqual(snapshot.frames[-1].reported_duration_us, 25200)
        self.unit.observe(11, sensor_ns + 25_016_000, 25016, self.unit.next_submission())
        restored = self.unit.snapshot().correction
        self.assertEqual(restored.state, "restored")
        self.assertEqual(restored.restored_timestamp_ns, sensor_ns + 25_016_000)
        self.unit.bind("camera-test", 2)
        self.assertEqual(len(self.unit.snapshot().frames), 2)

    def test_cancel_restores_baseline_without_owning_capture(self):
        self.unit.adjust(self.pulse)
        self.assertEqual(self.unit.next_submission().duration_us, 25200)
        self.unit.cancel()
        restore = self.unit.next_submission()
        self.assertTrue(restore.restoration)
        self.unit.observe(1, boot_time_ns(), 25000, restore)
        self.assertEqual(self.unit.snapshot().correction.state, "restored")
        self.assertEqual(self.unit.next_submission().duration_us, 25000)
        self.assertEqual(self.unit.fault(), "")


class BrowserPreviewTests(unittest.TestCase):
    def setUp(self):
        self.capture = SimpleNamespace(units={"board-a1": None, "board-a2": None},
                                       images={}, image_lock=threading.Lock())

        def encode(extension, image, options):
            output = BytesIO()
            Image.fromarray(image).save(output, format="JPEG")
            return True, np.frombuffer(output.getvalue(), dtype=np.uint8)

        self.capture.cv2 = SimpleNamespace(IMWRITE_JPEG_QUALITY=1, imencode=Mock(side_effect=encode))
        self.preview = BrowserPreview(self.capture, port=0, fps=30)
        self.preview.start()

    def tearDown(self):
        self.preview.close()
        self.assertFalse(self.preview.http_thread.is_alive())
        self.assertFalse(self.preview.encoder_thread.is_alive())

    def request(self, path):
        connection = HTTPConnection("127.0.0.1", self.preview.server.server_port, timeout=1)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def publish(self, value):
        with self.capture.image_lock:
            self.capture.images = {unit: (time.monotonic(), np.full((200, 1280), value, dtype=np.uint8))
                                   for unit in self.capture.units}

    def image(self, unit="board-a1", value=None):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            code, data = self.request(f"/preview/{unit}.jpg")
            if code == 200:
                with Image.open(BytesIO(data)) as decoded:
                    decoded.load()
                    self.assertEqual(decoded.size, (1280, 200))
                    if value is None or decoded.getpixel((0, 0)) == value:
                        return data
            else:
                self.assertEqual(code, 204)
            time.sleep(0.01)
        self.fail("Fresh JPEG did not arrive")

    def test_loopback_http_boards_status_updates_and_stale_images(self):
        self.assertEqual(self.preview.server.server_address[0], "127.0.0.1")
        code, page = self.request("/")
        self.assertEqual(code, 200)
        for unit in self.capture.units:
            self.assertIn(unit.encode(), page)
        self.assertEqual(self.request("/preview/board-a1.jpg")[0], 204)
        self.assertEqual(self.request("/preview/unknown.jpg")[0], 404)
        self.assertEqual(self.request("/unknown")[0], 404)
        self.preview.publish_status({"state": "calibrating", "ready": False})
        self.assertEqual(json.loads(self.request("/status")[1]), {"state": "calibrating", "ready": False})
        self.publish(64)
        for unit in self.capture.units:
            self.image(unit)
        # HTTP clients can disconnect; the source continues publishing new frames.
        self.publish(128)
        self.image(value=128)
        with self.capture.image_lock:
            self.capture.images.clear()
        time.sleep(1.05)
        self.assertEqual(self.request("/preview/board-a1.jpg")[0], 204)

    def test_slow_encoding_leaves_capture_lock_and_http_available(self):
        entered, release, published = threading.Event(), threading.Event(), threading.Event()
        encode = self.capture.cv2.imencode.side_effect

        def slow_encode(*args):
            entered.set()
            release.wait(3)
            return encode(*args)

        self.capture.cv2.imencode.side_effect = slow_encode
        self.publish(32)
        try:
            self.assertTrue(entered.wait(1))
            thread = threading.Thread(target=lambda: (self.publish(96), published.set()))
            thread.start()
            self.assertTrue(published.wait(1), "JPEG encoding held the capture image lock")
            thread.join()
            self.assertEqual(self.request("/status")[0], 200)
            self.assertEqual(self.request("/preview/board-a1.jpg")[0], 204)
        finally:
            release.set()
        self.image(value=96)

    def test_encoder_failure_is_reported(self):
        self.capture.cv2.imencode.side_effect = RuntimeError("encoder failed")
        self.publish(64)
        self.preview.encoder_thread.join(2)
        self.assertFalse(self.preview.encoder_thread.is_alive())
        with self.assertRaisesRegex(RuntimeError, "encoder failed"):
            self.preview.publish_status({"ready": True})


class PreviewCleanupTests(unittest.TestCase):
    def test_sync_stop_and_fault_leave_recording_running_until_application_end(self):
        config = Path(__file__).resolve().parents[1] / "examples/configs/fake_one_node.json"
        for fault in ("", "peer disconnected"):
            with self.subTest(fault=fault):
                capture = Mock(units={})
                session = Mock()
                session.wait_ready.return_value = True
                session.status.return_value = SimpleNamespace(
                    state="fault" if fault else "stopped", ready=False, fault=fault,
                    units=[], waiting_nodes=[], stop_node="node-b",
                    stop_reason="fault" if fault else "requested")
                context = Mock()
                context.__enter__ = Mock(return_value=session)
                context.__exit__ = Mock(return_value=False)
                elapsed = [0]

                def sleep(seconds):
                    if seconds == 1 and capture.start_recording.called:
                        # The application runs another iteration after synchronization exits.
                        self.assertFalse(capture.stop.called)
                        elapsed[0] += 1

                with patch("sys.argv", ["opencv_sync", str(config), "--seconds", "2", "--repeat", "1", "--video-dir", "/tmp/video"]), \
                        patch("opencv_sync.Capture", return_value=capture), \
                        patch("opencv_sync.SyncSession", return_value=context), \
                        patch("opencv_sync.time.monotonic", side_effect=lambda: elapsed[0]), \
                        patch("opencv_sync.time.sleep", side_effect=sleep):
                    main()
                self.assertEqual(elapsed[0], 2)
                capture.start_recording.assert_called_once()
                capture.stop.assert_called_once()

    def test_preview_cleanup_failure_still_stops_capture(self):
        config = Path(__file__).resolve().parents[1] / "examples/configs/fake_one_node.json"
        capture = Mock(units={})
        preview = Mock()
        preview.publish_status.side_effect = RuntimeError("encoder failed")
        preview.close.side_effect = OSError("HTTP shutdown failed")
        session = Mock()
        session.wait_ready.return_value = True
        session.status.return_value = SimpleNamespace(state="tracking", ready=True, fault="", units=[],
                                                       waiting_nodes=[], stop_node="", stop_reason="")
        context = Mock()
        context.__enter__ = Mock(return_value=session)
        context.__exit__ = Mock(return_value=False)
        with patch("sys.argv", ["opencv_sync", str(config), "--http-preview", "--continuous", "--repeat", "1"]), \
                patch("opencv_sync.Capture", return_value=capture), \
                patch("opencv_sync.BrowserPreview", return_value=preview), \
                patch("opencv_sync.SyncSession", return_value=context), \
                patch("opencv_sync.time.sleep"):
            with self.assertRaisesRegex(OSError, "HTTP shutdown failed"):
                main()
        capture.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
