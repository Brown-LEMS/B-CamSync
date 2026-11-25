"""Select one or two cameras and measure their startup success on a Pi."""

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
        frontend_timeout = any(b"Camera frontend has timed out" in line for line in output)

    if frontend_timeout:
        return False, "libcamera frontend timed out"
    if finished >= deadline or missing:
        reason = (f"{STARTUP_SECONDS}s first-frame deadline expired" if finished >= deadline
                  else f"example exited before first previews (exit {process.returncode})")
        if missing:
            reason += f" (missing preview from {', '.join(sorted(missing))})"
        return False, reason
    if not clean_stop:
        return False, f"example did not shut down cleanly (exit {process.returncode})"
    return True, f"first previews in {finished - started:.2f} s"


def choose(prompt, choices, count):
    while True:
        numbers = input(prompt).split()
        if len(numbers) == count and len(set(numbers)) == count and all(n in choices for n in numbers):
            return [choices[number] for number in numbers]
        print("Invalid selection. Choose from the listed numbers.")


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    modes = {"1": ("single_board_http.py", 1), "2": ("opencv_sync_http.py", 2)}
    [(example, count)] = choose("Mode (1 = single board, 2 = dual board): ", modes, 1)
    try:
        listing = subprocess.run([sys.executable, str(EXAMPLES / "single_board_http.py"), "--list"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                 check=True, timeout=STARTUP_SECONDS)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print("Could not list libcamera cameras.", file=sys.stderr)
        return 1
    camera_ids = listing.stdout.splitlines()
    if len(camera_ids) < count:
        print(f"Found {len(camera_ids)} cameras. This mode needs {count}.", file=sys.stderr)
        return 1

    choices = {str(index): camera_id for index, camera_id in enumerate(camera_ids, 1)}
    for number, camera_id in choices.items():
        print(f"  {number}. {camera_id}")
    prompt = "Camera number: " if count == 1 else "Two camera numbers (reference first, e.g. 1 2): "
    selected = choose(prompt, choices, count)
    command = [sys.executable, "-u", str(EXAMPLES / example), *selected]

    boards = tuple(f"board-{index}" for index in range(1, count + 1))
    successes = 0
    print(f"Running {ROUNDS} startup rounds with {', '.join(selected)}", flush=True)
    for number in range(1, ROUNDS + 1):
        passed, summary = trial(command, boards)
        successes += passed
        result = "PASS" if passed else "FAIL"
        print(f"[{number}/{ROUNDS}] {result} — {summary}", flush=True)
        if number < ROUNDS:
            time.sleep(PAUSE_SECONDS)

    print(f"Startup success: {successes}/{ROUNDS} ({successes / ROUNDS:.0%})", flush=True)
    return int(successes != ROUNDS)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EOFError, KeyboardInterrupt):
        print("\nStartup check cancelled.", file=sys.stderr)
        raise SystemExit(130)
