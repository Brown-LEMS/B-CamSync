"""Standalone developer preview checks; no synchronization or camera package imports."""

from io import BytesIO
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import remote_preview


def options(**changes):
    values = dict(host="pi-test", attach=False, remote_dir="/srv/camera checkout",
                  remote_python=".venv/bin/python", config="group.json", remote_port=8765,
                  local_port=0, fps=30, width=960, startup_timeout=2)
    values.update(changes)
    return SimpleNamespace(**values)


class ProtocolTests(unittest.TestCase):
    def test_board_identifiers_are_discovered_before_synchronization_is_ready(self):
        parser = remote_preview.PreviewUnits()
        parser.feed('<img data-unit="board &amp; one"><img data-unit="board-b"><img data-unit="board-b">')
        self.assertEqual(parser.units, ["board & one", "board-b"])

    def test_launch_quotes_remote_paths_and_keeps_all_forwarding_on_loopback(self):
        args = options(config="group 'name' $(touch wrong).json")
        command = remote_preview.ssh_command(args, 23456)
        self.assertIn("127.0.0.1:23456:127.0.0.1:8765", command)
        self.assertIn("-tt", command)
        self.assertIn("BatchMode=yes", command)
        self.assertIn("ControlPath=none", command)
        self.assertEqual(command[-2], "pi-test")
        self.assertEqual(command[-1],
                         "cd '/srv/camera checkout' && exec .venv/bin/python -u tools/opencv_capture.py "
                         "--http-preview --continuous --repeat 1 --http-port 8765 --preview-fps 30 "
                         "-- 'group '\"'\"'name'\"'\"' $(touch wrong).json'")
        attach = remote_preview.ssh_command(options(attach=True), 23456)
        self.assertEqual(attach[-4:], ["-N", "-T", "--", "pi-test"])

    def test_stale_frame_ends_relay_instead_of_repeating_old_image(self):
        done, errors = threading.Event(), queue.Queue()
        player = SimpleNamespace(stdin=BytesIO())
        with patch.object(remote_preview, "fetch", side_effect=[(200, b"jpeg"), (204, b"")]) as fetch:
            remote_preview.relay(12345, "board / one", player, done, errors, options())
        self.assertEqual(player.stdin.getvalue(), b"jpeg")
        self.assertIn("stale", str(errors.get_nowait()))
        self.assertEqual(fetch.call_args.args, (12345, "/preview/board%20%2F%20one.jpg"))

    def test_initial_empty_preview_can_wait_for_its_first_frame(self):
        done, errors = threading.Event(), queue.Queue()
        player = SimpleNamespace(stdin=BytesIO())

        calls = 0

        def fetch(*_):
            nonlocal calls
            calls += 1
            if calls == 1:
                return 204, b""
            done.set()
            return 200, b"first-frame"

        with patch.object(remote_preview, "fetch", side_effect=fetch):
            remote_preview.relay(12345, "board", player, done, errors, options())
        self.assertTrue(errors.empty())
        self.assertEqual(calls, 2)
        self.assertEqual(player.stdin.getvalue(), b"first-frame")


FAKE_SSH = '''
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json, os, pathlib, signal, sys, threading, time
root = pathlib.Path(os.environ["PREVIEW_TEST_DIRECTORY"])
(root / "ssh-arguments.json").write_text(json.dumps(sys.argv[1:]))
if os.environ.get("PREVIEW_TEST_SSH_EXIT"):
    print("Simulated SSH authentication failure", flush=True)
    sys.exit(3)
port = int(sys.argv[sys.argv.index("-L") + 1].split(":")[1])
payload = b"JPEG-PREVIEW" * 40
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self):
        if self.path == "/":
            data = b'<img data-unit="board-a"><img data-unit="board-b">'
        elif self.path == "/status":
            data = b'{"state":"tracking","ready":true}'
        elif self.path.startswith("/preview/"):
            if os.environ.get("PREVIEW_TEST_FAILURE"):
                deadline = time.monotonic() + 2
                while not all((root / (unit + ".ready")).exists() for unit in ("board-a", "board-b")):
                    if time.monotonic() >= deadline: break
                    time.sleep(.01)
                self.send_error(503)
                return
            data = payload
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
thread.start()
def terminate(*_):
    (root / "ssh-terminated").touch()
    raise SystemExit(0)
signal.signal(signal.SIGTERM, terminate)
print("Browser preview: http://127.0.0.1:8765", flush=True)
try:
    while True:
        data = os.read(0, 1)
        if data == b"\\x03":
            (root / "remote-interrupted").touch()
            break
        if not data:
            break
finally:
    server.shutdown()
    server.server_close()
    thread.join()
'''

FAKE_PLAYER = '''
import os, pathlib, signal, sys
root = pathlib.Path(os.environ["PREVIEW_TEST_DIRECTORY"])
title = sys.argv[sys.argv.index("-window_title") + 1]
unit = title.split(" / ", 1)[1].split(" ", 1)[0]
def terminate(*_):
    (root / (unit + ".terminated")).touch()
    raise SystemExit(0)
signal.signal(signal.SIGTERM, terminate)
(root / (unit + ".ready")).touch()
with (root / (unit + ".mjpeg")).open("wb") as output:
    count = 0
    while count < 2000:
        data = os.read(0, 8192)
        if not data:
            break
        output.write(data)
        output.flush()
        count += len(data)
'''


class ProcessTests(unittest.TestCase):
    def run_tool(self, attach=False, failure=False, ssh_failure=False):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for name, source in (("ssh", FAKE_SSH), ("ffplay", FAKE_PLAYER)):
            path = root / name
            path.write_text(f"#!{sys.executable}\n" + source)
            path.chmod(0o700)
        environment = dict(os.environ, PATH=str(root) + os.pathsep + os.environ.get("PATH", ""),
                           PREVIEW_TEST_DIRECTORY=str(root))
        if failure:
            environment["PREVIEW_TEST_FAILURE"] = "1"
        if ssh_failure:
            environment["PREVIEW_TEST_SSH_EXIT"] = "1"
        command = [sys.executable, str(Path(remote_preview.__file__).resolve()), "pi-test",
                   "--fps", "30", "--startup-timeout", "2"]
        command += ["--attach"] if attach else ["--remote-dir", "/srv/camera checkout"]
        result = subprocess.run(command, cwd=root, env=environment, capture_output=True,
                                text=True, timeout=10)
        return root, result

    def test_launch_relays_each_board_and_interrupts_only_its_owned_remote_application(self):
        root, result = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((root / "remote-interrupted").exists())
        for unit in ("board-a", "board-b"):
            self.assertIn(b"JPEG-PREVIEW", (root / f"{unit}.mjpeg").read_bytes())
        self.assertIn("Tunnel ready", result.stdout)
        command = json.loads((root / "ssh-arguments.json").read_text())
        self.assertIn("-tt", command)

    def test_attach_closes_tunnel_without_sending_remote_interrupt(self):
        root, result = self.run_tool(attach=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((root / "remote-interrupted").exists())
        self.assertTrue((root / "ssh-terminated").exists())
        self.assertIn('"ready": true', result.stdout)

    def test_failed_ssh_start_reports_error_without_starting_players(self):
        root, result = self.run_tool(ssh_failure=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("exited (3)", result.stderr)
        self.assertIn("authentication failure", result.stderr)
        self.assertFalse((root / "remote-interrupted").exists())
        self.assertEqual(list(root.glob("*.mjpeg")), [])

    def test_http_failure_closes_players_and_its_owned_remote_application(self):
        root, result = self.run_tool(failure=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("HTTP 503", result.stderr)
        self.assertTrue((root / "remote-interrupted").exists())
        self.assertTrue((root / "board-a.terminated").exists())
        self.assertTrue((root / "board-b.terminated").exists())


if __name__ == "__main__":
    unittest.main()
