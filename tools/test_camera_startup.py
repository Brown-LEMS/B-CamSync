"""Check startup measurement with isolated preview processes and simulated frames."""

from contextlib import redirect_stdout
from io import StringIO
import json
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
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("camera_ids", nargs="+")
parser.add_argument("--bind")
parser.add_argument("--port", type=int)
args = parser.parse_args()
for index, camera_id in enumerate(args.camera_ids, 1):
    print(f"board-{index}: {camera_id}", flush=True)
    event = dict(unit=f"board-{index}", camera_id=camera_id, attempt=1, valid_frames=0,
                 first_frame_ms=None, total_ms=100, elapsed_ms=100)
    if camera_id == "exhausted":
        print("B-CamSync startup " + json.dumps(dict(event, outcome="exhausted", reason="cancelled")), flush=True)
        raise SystemExit(1)
    if camera_id == "recovered":
        print("Camera frontend has timed out!", flush=True)
        print("B-CamSync startup " + json.dumps(dict(event, outcome="retry", reason="cancelled")), flush=True)
        event.update(attempt=2, total_ms=200)
    print("B-CamSync startup " + json.dumps(dict(event, outcome="ready", reason="", valid_frames=3)), flush=True)
if "watchdog" in args.camera_ids:
    print("Camera frontend has timed out!", flush=True)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/status":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"ready": "no-sync" not in args.camera_ids, "fault": ""}).encode())
            return
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

    def measure(self, second_id, *, sync_timeout=2, round_number=1):
        cameras = {"board-1": "camera-a", "board-2": second_id}
        return check_camera_startup.measured_round(round_number, [*self.command, *cameras.values()], cameras,
                                                   self.released.parent, 3, sync_timeout)

    def test_measurement_records_distinct_camera_and_sync_milestones(self):
        record = self.measure("camera-b")
        self.assertEqual(record["outcome"], "ready")
        self.assertEqual(record["frontend_timeouts"], 0)
        self.assertEqual(record["both_jpegs_ms"], max(record["jpeg_ms"].values()))
        self.assertGreaterEqual(record["sync_ready_ms"], record["both_jpegs_ms"])
        self.assertTrue(record["status"]["ready"])
        self.assertTrue(record["clean_stop"])
        self.assertTrue((self.released.parent / record["log"]).is_file())

    def test_measurement_preserves_recovery_events(self):
        record = self.measure("recovered")
        self.assertEqual(record["outcome"], "ready")
        self.assertEqual(record["frontend_timeouts"], 1)
        self.assertEqual([(e["unit"], e["attempt"], e["outcome"]) for e in record["events"]],
                         [("board-1", 1, "ready"), ("board-2", 1, "retry"), ("board-2", 2, "ready")])

    def test_measurement_separates_camera_exhaustion_from_sync_deadline(self):
        exhausted = self.measure("exhausted")
        self.assertEqual(exhausted["outcome"], "camera_exit")
        self.assertIsNone(exhausted["both_jpegs_ms"])
        self.assertEqual(exhausted["events"][-1]["outcome"], "exhausted")
        waiting = self.measure("no-sync", sync_timeout=0.15, round_number=2)
        self.assertEqual(waiting["outcome"], "sync_timeout")
        self.assertIsNotNone(waiting["both_jpegs_ms"])
        self.assertIsNone(waiting["sync_ready_ms"])

    def test_noninteractive_run_persists_each_failed_round(self):
        directory = self.released.parent
        (directory / "opencv_sync_http.py").write_text(FAKE_PREVIEW)
        args = ["check", "--camera-ids", "camera-a", "exhausted", "--rounds", "2", "--output-dir", str(directory)]
        with (patch.object(sys, "argv", args), patch.object(check_camera_startup, "EXAMPLES", directory),
              patch.object(check_camera_startup, "PAUSE_SECONDS", 0), redirect_stdout(StringIO())):
            self.assertEqual(check_camera_startup.main(), 1)
        rounds = [json.loads(line) for line in (directory / "rounds.jsonl").read_text().splitlines()]
        self.assertEqual([(r["round"], r["outcome"]) for r in rounds], [(1, "camera_exit"), (2, "camera_exit")])
        self.assertTrue(all((directory / r["log"]).is_file() for r in rounds))


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
