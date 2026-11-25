"""Check startup measurement with isolated preview processes and simulated frames."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import _camera_startup


FAKE_PREVIEW = r"""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer

parser = argparse.ArgumentParser()
parser.add_argument("camera_ids", nargs="+")
parser.add_argument("--bind")
parser.add_argument("--port", type=int)
args = parser.parse_args()
for index, camera_id in enumerate(args.camera_ids, 1):
    print(f"board-{index}: {camera_id}", flush=True)
if "watchdog" in args.camera_ids:
    print("Camera frontend has timed out!", flush=True)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        index = int(self.path.removeprefix("/board-").removesuffix(".jpg")) - 1
        available = index < len(args.camera_ids) and args.camera_ids[index] != "missing"
        self.send_response(200 if available else 204)
        self.end_headers()
        if available:
            self.wfile.write(b"\xff\xd8preview\xff\xd9")

with HTTPServer((args.bind, args.port), Handler) as server:
    try:
        server.serve_forever(poll_interval=0.01)
    except KeyboardInterrupt:
        print("Released cameras")
"""


class CameraStartupTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        preview = Path(directory.name) / "preview.py"
        preview.write_text(FAKE_PREVIEW)
        self.command = [sys.executable, "-u", str(preview)]

    def test_one_board_passes_after_preview(self):
        failure, lines = _camera_startup.trial([*self.command, "camera-a"], ("board-1",))
        self.assertIsNone(failure)
        self.assertIn("board-1: camera-a", lines)

    def test_dual_board_requires_both_previews(self):
        boards = ("board-1", "board-2")
        failure, lines = _camera_startup.trial([*self.command, "camera-a", "camera-b"], boards)
        self.assertIsNone(failure)
        self.assertIn("board-2: camera-b", lines)
        with patch.object(_camera_startup, "STARTUP_SECONDS", 1):
            failure, lines = _camera_startup.trial([*self.command, "camera-a", "missing"], boards)
        self.assertIn("deadline expired; missing preview from board-2", failure)
        self.assertIn("Released cameras", lines)

    def test_frontend_timeout_counts_as_failure_even_with_a_jpeg(self):
        failure, lines = _camera_startup.trial([*self.command, "watchdog"], ("board-1",))
        self.assertEqual(failure, "libcamera frontend timed out")
        self.assertIn("Camera frontend has timed out!", lines)
        self.assertIn("Released cameras", lines)

    def test_reports_success_ratio_after_ten_rounds_in_supplied_order(self):
        with (patch.object(sys, "argv", ["check", "camera-b", "camera-a"]),
              patch.object(_camera_startup, "PAUSE_SECONDS", 0),
              patch.object(_camera_startup, "trial", side_effect=[(None, [])] * 8 +
                           [("first-frame failure", ["camera log"])] * 2) as trial,
              redirect_stdout(StringIO()) as output):
            code = _camera_startup.main("opencv_sync_http.py", 2)
        self.assertEqual(code, 1)
        self.assertEqual(trial.call_count, 10)
        for command, boards in (call.args for call in trial.call_args_list):
            self.assertEqual(command[-2:], ["camera-b", "camera-a"])
            self.assertEqual(boards, ("board-1", "board-2"))
        self.assertIn("[10/10]", output.getvalue())
        self.assertIn("camera log", output.getvalue())
        self.assertIn("Startup success: 8/10 (80%)", output.getvalue())
