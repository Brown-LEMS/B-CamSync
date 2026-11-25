"""Run fresh HTTP preview processes and check first-frame delivery."""

import argparse
from contextlib import closing
from http.client import HTTPConnection, HTTPException
from pathlib import Path
import signal
import socket
import subprocess
import sys
from tempfile import TemporaryFile
import time


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
HOST = "127.0.0.1"
ROUNDS = 10
STARTUP_SECONDS = 16
PAUSE_SECONDS = 0.5


def has_jpeg(port, board):
    with closing(HTTPConnection(HOST, port, timeout=0.25)) as connection:
        try:
            connection.request("GET", f"/{board}.jpg")
            response = connection.getresponse()
            if response.status == 200:
                image = response.read()
                return image.startswith(b"\xff\xd8") and image.endswith(b"\xff\xd9")
        except (OSError, HTTPException):
            pass  # The example may still be starting or exiting.
    return False


def stop(process):
    """Request the example's normal Ctrl+C cleanup and reap the process."""
    if process.poll() is not None:
        return False
    process.send_signal(signal.SIGINT)
    try:
        return process.wait(timeout=5) == 0
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return False


def trial(command, boards):
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        port = probe.getsockname()[1]

    missing = set(boards)
    with TemporaryFile() as output:
        started = time.monotonic()
        deadline = started + STARTUP_SECONDS
        process = subprocess.Popen([*command, "--bind", HOST, "--port", str(port)],
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while process.poll() is None and time.monotonic() < deadline:
                missing = {board for board in sorted(missing) if not has_jpeg(port, board)}
                if not missing:
                    break
                time.sleep(0.05)
            finished = time.monotonic()
        finally:
            clean_stop = stop(process)

        output.seek(0)
        log = output.read().decode(errors="replace").splitlines()

    if any("Camera frontend has timed out" in line for line in log):
        return "libcamera frontend timed out", log
    if finished >= deadline or missing:
        reason = (f"{STARTUP_SECONDS}s first-frame deadline expired" if finished >= deadline
                  else f"example exited before first previews (exit {process.returncode})")
        if missing:
            reason += f"; missing preview from {', '.join(sorted(missing))}"
        return reason, log
    if not clean_stop:
        return f"example did not shut down cleanly (exit {process.returncode})", log
    selection = [line for line in log if line.startswith(("Board:", "board-"))]
    return None, [*selection, f"First previews in {finished - started:.2f} s"]


def main(example, count):
    parser = argparse.ArgumentParser(description=f"Run {ROUNDS} starts of {example}.")
    parser.add_argument("camera_ids", nargs="*", metavar="CAMERA_ID",
                        help=f"{count} camera ID(s); defaults to the example's selection")
    parser.add_argument("--list", action="store_true", help="list camera IDs without starting capture")
    args = parser.parse_args()
    if args.camera_ids and (len(args.camera_ids) != count or len(set(args.camera_ids)) != count):
        parser.error(f"provide either no camera IDs or {count} distinct camera ID(s)")
    command = [sys.executable, "-u", str(EXAMPLES / example), *args.camera_ids]
    if args.list:
        return subprocess.call([*command, "--list"])

    boards = tuple(f"board-{index}" for index in range(1, count + 1))
    successes = 0
    for number in range(1, ROUNDS + 1):
        print(f"[{number}/{ROUNDS}] Starting {example}", flush=True)
        failure, lines = trial(command, boards)
        if failure is None:
            successes += 1
            print("  PASS", flush=True)
        else:
            print(f"  FAIL: {failure}", flush=True)
        for line in lines:
            print(f"    {line}", flush=True)
        if number < ROUNDS:
            time.sleep(PAUSE_SECONDS)

    print(f"Startup success: {successes}/{ROUNDS} ({successes / ROUNDS:.0%})", flush=True)
    return int(successes != ROUNDS)
