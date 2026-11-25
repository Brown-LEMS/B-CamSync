"""Check startup measurement with isolated preview processes and simulated frames."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import check_camera_startup


FAKE_PREVIEW = r"""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

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
        Path(__file__).with_suffix(".released").touch()
"""


class CameraStartupTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        preview = Path(directory.name) / "preview.py"
        preview.write_text(FAKE_PREVIEW)
        self.released = preview.with_suffix(".released")
        self.command = [sys.executable, "-u", str(preview)]

    def test_one_board_passes_after_preview(self):
        passed, summary = check_camera_startup.trial([*self.command, "camera-a"], ("board-1",))
        self.assertTrue(passed)
        self.assertIn("first previews in", summary)
        self.assertTrue(self.released.is_file())

    def test_dual_board_requires_both_previews(self):
        boards = ("board-1", "board-2")
        passed, _ = check_camera_startup.trial([*self.command, "camera-a", "camera-b"], boards)
        self.assertTrue(passed)
        self.released.unlink()
        with patch.object(check_camera_startup, "STARTUP_SECONDS", 1):
            passed, summary = check_camera_startup.trial([*self.command, "camera-a", "missing"], boards)
        self.assertFalse(passed)
        self.assertIn("deadline expired (missing preview from board-2)", summary)
        self.assertTrue(self.released.is_file())

    def test_frontend_timeout_counts_as_failure_even_with_a_jpeg(self):
        passed, summary = check_camera_startup.trial([*self.command, "watchdog"], ("board-1",))
        self.assertFalse(passed)
        self.assertEqual(summary, "libcamera frontend timed out")
        self.assertTrue(self.released.is_file())


class CameraStartupMenuTests(unittest.TestCase):
    def run_menu(self, inputs, outcomes):
        listing = subprocess.CompletedProcess([], 0, stdout="camera-a\ncamera-b\n")
        with (patch.object(sys, "argv", ["check"]),
              patch("builtins.input", side_effect=inputs),
              patch.object(check_camera_startup, "PAUSE_SECONDS", 0),
              patch.object(check_camera_startup.subprocess, "run", return_value=listing) as discover,
              patch.object(check_camera_startup, "trial", side_effect=outcomes) as trial,
              redirect_stdout(StringIO()) as output):
            code = check_camera_startup.main()
        discover.assert_called_once()
        self.assertEqual(discover.call_args.args[0][-1], "--list")
        self.assertEqual(discover.call_args.kwargs["stderr"], subprocess.DEVNULL)
        return code, output.getvalue(), trial.call_args_list

    def test_single_board_can_be_selected_by_number(self):
        code, output, calls = self.run_menu(["1", "2"], [(True, "first previews in 0.20 s")] * 10)
        self.assertEqual(code, 0)
        self.assertIn("  2. camera-b", output)
        self.assertIn("Startup success: 10/10 (100%)", output)
        self.assertEqual(len(calls), 10)
        for command, boards in (call.args for call in calls):
            self.assertEqual(Path(command[-2]).name, "single_board_http.py")
            self.assertEqual(command[-1], "camera-b")
            self.assertEqual(boards, ("board-1",))

    def test_dual_board_preserves_selection_order_and_prints_one_line_per_round(self):
        outcomes = [(True, "first previews in 0.20 s")] * 8 + [(False, "libcamera frontend timed out")] * 2
        code, output, calls = self.run_menu(["2", "2 2", "2 1"], outcomes)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 10)
        for command, boards in (call.args for call in calls):
            self.assertEqual(Path(command[-3]).name, "opencv_sync_http.py")
            self.assertEqual(command[-2:], ["camera-b", "camera-a"])
            self.assertEqual(boards, ("board-1", "board-2"))
        self.assertEqual(output.splitlines(), [
            "  1. camera-a",
            "  2. camera-b",
            "Invalid selection. Choose from the listed numbers.",
            "Running 10 startup rounds with camera-b, camera-a",
            *(f"[{number}/10] PASS — first previews in 0.20 s" for number in range(1, 9)),
            "[9/10] FAIL — libcamera frontend timed out",
            "[10/10] FAIL — libcamera frontend timed out",
            "Startup success: 8/10 (80%)",
        ])
