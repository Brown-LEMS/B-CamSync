"""Exercise remote downstream MJPEG recording with two synthetic boards."""

from http.client import HTTPConnection
from io import BytesIO
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from bcamsync import Config
from bcamsync.management import NodeService

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
from preview_record import RecordingService
import preview_record


class RecordingExampleTests(unittest.TestCase):
    def setUp(self):
        self.output = TemporaryDirectory()
        self.config = Config.from_file(ROOT / "examples/configs/fake_one_node.json")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
            channel.bind(("127.0.0.1", 0))
            port = channel.getsockname()[1]
        self.controller = NodeService(self.config, "node-a")
        self.service = RecordingService(self.controller, "127.0.0.1", output_dir=self.output.name,
                                        http_port=0, discovery_port=port, preview_fps=1, max_width=32)
        self.failures = []

        def serve():
            try:
                self.service.serve()
            except BaseException as error:
                self.failures.append(error)

        self.thread = threading.Thread(target=serve)
        self.thread.start()
        self.assertTrue(self.service.listening.wait(5), str(self.failures))

    def tearDown(self):
        self.service.request_shutdown()
        self.thread.join(15)
        self.assertFalse(self.thread.is_alive())
        self.service.close()
        self.controller.close()
        self.output.cleanup()

    def request(self, path, method="POST", body=b"{}"):
        connection = HTTPConnection(self.service.address, self.service.http_port, timeout=10)
        try:
            connection.request(method, path, body=body if method == "POST" else None,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def control(self, path):
        code, payload = self.request(path)
        self.assertEqual(code, 200, payload)
        return json.loads(payload)["result"]

    def wait_frames(self, count=8):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            stats = self.service.recording_status()
            if all(unit["frames"] >= count for unit in stats["units"].values()):
                return stats
            time.sleep(0.02)
        self.fail(f"Recording did not receive both boards: {stats}, {self.failures}")

    def validate_files(self, recording):
        directory = Path(recording["directory"])
        manifest = json.loads((directory / "manifest.json").read_text())
        self.assertEqual(set(manifest["units"]), {"board-a1", "board-a2"})
        self.assertEqual(json.loads((directory / "summary.json").read_text()), recording)
        for unit_id, files in manifest["units"].items():
            metadata = [json.loads(line) for line in (directory / files["metadata"]).read_text().splitlines()]
            video = (directory / files["video"]).read_bytes()
            stats = recording["units"][unit_id]
            self.assertEqual(len(metadata), stats["frames"])
            self.assertEqual(len(video), stats["bytes"])
            self.assertGreaterEqual(len(metadata), 8)
            offset = 0
            for index, frame in enumerate(metadata):
                self.assertEqual(frame["offset"], offset)
                self.assertGreater(frame["sensor_timestamp_ns"], 0)
                self.assertTrue(frame["clock_valid"])
                self.assertEqual(frame["view_shape"], [16, 16])
                self.assertEqual(frame["views"], 4)
                with Image.open(BytesIO(video[offset:offset + frame["length"]])) as image:
                    image.load()
                    self.assertEqual(image.format, "JPEG")
                    self.assertEqual(image.mode, "L")
                    self.assertEqual(image.size, (64, 16))
                if index:
                    self.assertGreater(frame["sequence"], metadata[index - 1]["sequence"])
                offset += frame["length"]
            self.assertEqual(offset, len(video))

    def test_remote_preview_record_stop_and_restart(self):
        self.assertEqual(self.request("/record/start")[0], 409)
        self.control("/start")
        self.control("/record/start")
        self.assertEqual(self.request("/record/start")[0], 409)
        self.wait_frames()
        code, image = self.request("/preview/board-a2.jpg", "GET")
        self.assertEqual(code, 200)
        with Image.open(BytesIO(image)) as preview:
            self.assertEqual(preview.size, (32, 8))
        stopped = self.control("/record/stop")["recording"]
        self.assertFalse(stopped["active"])
        self.validate_files(stopped)
        status = json.loads(self.request("/status", "GET")[1])["result"]
        self.assertTrue(status["active"])
        self.assertEqual(status["recording"], stopped)
        time.sleep(0.05)
        self.assertEqual(self.service.recording_status(), stopped)
        self.control("/record/start")
        self.wait_frames()
        stopped_again = self.control("/stop")["recording"]
        self.assertNotEqual(stopped_again["directory"], stopped["directory"])
        self.validate_files(stopped_again)
        self.control("/start")
        self.control("/record/start")
        self.wait_frames()
        self.validate_files(self.control("/record/stop")["recording"])
        self.assertEqual(self.failures, [])

    def test_recording_request_fields_are_rejected(self):
        for body in (b"null", b"[]", b"broken", b'{"operation_id":"ignored"}'):
            self.assertEqual(self.request("/record/start", body=body)[0], 400)

    def test_disk_error_is_visible_and_ends_producers(self):
        self.control("/start")
        self.control("/record/start")
        with self.service._record_lock:
            _, metadata = self.service._streams["board-a1"]
            broken = Mock()
            broken.tell.return_value = 0
            broken.write.side_effect = OSError("disk full")
            self.service._streams["board-a1"] = broken, metadata
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertIn("disk full", self.service.recording_status()["fault"])
        self.assertIn("disk full", str(self.failures))
        self.service.close()
        self.assertFalse(self.service.recording_status()["active"])

    def test_inflight_encoder_cannot_write_into_restarted_recording(self):
        entered, release = threading.Event(), threading.Event()
        old_sequences = []
        original_encode = preview_record.encode_packet

        def paused_encode(packet, width, quality):
            if packet.unit_id == "board-a1" and not old_sequences:
                old_sequences.append(packet.sequence)
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("encoder was not released")
            return original_encode(packet, width, quality)

        self.control("/start")
        # A Mock call history would retain packets and exhaust the bounded image pool.
        with patch.object(preview_record, "encode_packet", new=paused_encode):
            try:
                self.control("/record/start")
                self.assertTrue(entered.wait(5))
                self.control("/record/stop")
                self.control("/record/start")
                release.set()
                self.wait_frames(count=max(unit.pool_capacity for unit in self.config.units) + 4)
                recording = self.control("/record/stop")["recording"]
            finally:
                release.set()
        directory = Path(recording["directory"])
        manifest = json.loads((directory / "manifest.json").read_text())
        frames = [json.loads(line) for line in
                  (directory / manifest["units"]["board-a1"]["metadata"]).read_text().splitlines()]
        self.assertGreater(frames[0]["sequence"], old_sequences[0])
        self.validate_files(recording)
        self.assertEqual(self.failures, [])

    def test_shutdown_closes_recording_files(self):
        self.control("/start")
        self.control("/record/start")
        self.wait_frames()
        self.service.request_shutdown()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.service.close()
        recording = self.service.recording_status()
        self.assertFalse(recording["active"])
        self.validate_files(recording)
        self.assertEqual(self.failures, [])

    def test_pi5_template_selects_two_pi5_boards(self):
        config = Config.from_file(ROOT / "examples/configs/one_node_two_boards_pi5.json")
        self.assertEqual(len(config.nodes), 1)
        self.assertEqual(len(config.units), 2)
        self.assertTrue(all(unit.profile == "arducam_b0267_pi5" for unit in config.units))


if __name__ == "__main__":
    unittest.main()
