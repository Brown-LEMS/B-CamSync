"""The independent preview application consumes real fake-backend frames."""

from http.client import HTTPConnection
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from bcamsync import Config
from bcamsync.management import NodeService


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
spec = importlib.util.spec_from_file_location("preview_example", ROOT / "examples" / "preview.py")
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


class PreviewApplicationTests(unittest.TestCase):
    def setUp(self):
        config = Config.from_file(ROOT / "examples" / "configs" / "fake_one_node.json")
        units = config.units
        for unit in units:
            fake = unit.fake
            fake.initial_phase_us = 0
            fake.drift_ppm = 0
            unit.fake = fake
        config.units = units
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
            channel.bind(("127.0.0.1", 0))
            discovery_port = channel.getsockname()[1]
        self.controller = NodeService(config, "node-a")
        self.service = preview.PreviewService(self.controller, "127.0.0.1", http_port=0,
                                              discovery_port=discovery_port, max_width=32)
        self.failures = []

        def serve():
            try:
                self.service.serve()
            except BaseException as error:
                self.failures.append(error)

        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        self.assertTrue(self.service.listening.wait(5), f"HTTP application did not listen: {self.failures}")

    def tearDown(self):
        self.service.request_shutdown()
        self.thread.join(timeout=15)
        self.assertFalse(self.thread.is_alive(), "Preview application did not terminate")
        self.assertEqual(self.failures, [])
        self.assertTrue(all(not thread.is_alive() for thread in self.service._producers))
        self.controller.close()

    def request(self, path, method="GET"):
        connection = HTTPConnection(self.service.address, self.service.http_port, timeout=5)
        try:
            connection.request(method, path, body=b"{}" if method == "POST" else None,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def image(self, unit="board-a1", timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code, encoded = self.request(f"/preview/{unit}.jpg")
            if code == 200:
                return encoded
            self.assertEqual(code, 204)
            time.sleep(0.05)
        self.fail(f"No preview arrived for {unit}: {self.failures}")

    def test_http_controls_geometry_inactive_clear_and_restart(self):
        code, page = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Start node", page)
        self.assertIn(b"/preview/", page)
        self.assertIsNone(self.service.session)
        self.assertEqual(self.request("/preview/board-a1.jpg")[0], 204)
        self.assertEqual(self.request("/start", "POST")[0], 200)
        for unit in ("board-a1", "board-a2"):
            with Image.open(BytesIO(self.image(unit))) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(image.mode, "L")
                self.assertEqual(image.size, (32, 8))
        # Each HTTP request and browser snapshot closes its connection; the
        # acquisition remains active and later previews are still produced.
        status = json.loads(self.request("/status")[1])["result"]
        self.assertTrue(status["active"])
        session = self.service.session
        self.assertEqual(self.request("/stop", "POST")[0], 200)
        self.assertEqual(self.request("/preview/board-a1.jpg")[0], 204)
        self.assertEqual(self.request("/preview/board-a2.jpg")[0], 204)
        self.assertEqual(self.request("/start", "POST")[0], 200)
        self.assertIs(self.service.session, session)
        with Image.open(BytesIO(self.image())) as image:
            self.assertEqual(image.size, (32, 8))

    def test_sync_fault_status_does_not_gate_local_pixels(self):
        original_request = self.service.request

        def status_with_sync_fault(command, operation_id=None):
            result = original_request(command, operation_id)
            if command == "status" and result["active"]:
                result = dict(result, ready=False, fault="simulated synchronization fault")
            return result

        with patch.object(self.service, "request", side_effect=status_with_sync_fault):
            self.assertEqual(self.request("/start", "POST")[0], 200)
            with Image.open(BytesIO(self.image())) as image:
                self.assertEqual(image.size, (32, 8))
            status = json.loads(self.request("/status")[1])["result"]
            self.assertFalse(status["ready"])
            self.assertEqual(status["fault"], "simulated synchronization fault")
            self.assertTrue(status["active"])

    def test_old_encoder_cannot_publish_after_stop_and_restart(self):
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        old = threading.Event()
        original_encode = preview.encode_packet

        def paused_encode(packet, width, quality):
            if packet.unit_id == "board-a1" and not old.is_set():
                old.set()
                entered.set()
                release.wait(10)
                completed.set()
                return b"old-session-jpeg"
            return original_encode(packet, width, quality)

        with patch.object(preview, "encode_packet", side_effect=paused_encode):
            try:
                self.assertEqual(self.request("/start", "POST")[0], 200)
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.request("/stop", "POST")[0], 200)
                self.assertEqual(self.request("/start", "POST")[0], 200)
                release.set()
                self.assertTrue(completed.wait(5))
                encoded = self.image(timeout=5)
                self.assertNotEqual(encoded, b"old-session-jpeg")
                with Image.open(BytesIO(encoded)) as image:
                    self.assertEqual(image.size, (32, 8))
            finally:
                release.set()

    def test_partial_producer_start_failure_joins_started_threads(self):
        service = preview.PreviewService(self.controller, "127.0.0.1", http_port=0,
                                        discovery_port=self.service.discovery_port)
        original_start = threading.Thread.start

        def start(thread):
            if thread.name == "preview-board-a2":
                raise RuntimeError("preview thread start failed")
            return original_start(thread)

        with patch.object(threading.Thread, "start", start):
            with self.assertRaisesRegex(RuntimeError, "preview thread start failed"):
                service.serve()
        self.assertTrue(all(not thread.is_alive() for thread in service._producers))
        # A preview startup failure leaves its caller's controller usable.
        self.assertFalse(self.controller.request("status")["active"])


if __name__ == "__main__":
    unittest.main()
