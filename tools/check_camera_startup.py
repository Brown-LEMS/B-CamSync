"""Select one or two cameras and measure their startup success on a Pi."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException
import json
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


def fetch(port, path):
    with closing(HTTPConnection(HOST, port, timeout=0.25)) as connection:
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            if response.status == 200:
                return response.read()
        except (OSError, HTTPException):
            pass  # The example may still be starting or exiting.
    return b""


def has_jpeg(port, board):
    image = fetch(port, f"/{board}.jpg")
    return image.startswith(b"\xff\xd8") and image.endswith(b"\xff\xd9")


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


def local_port():
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def trial(command, boards):
    port = local_port()
    missing = set(boards)
    with TemporaryFile() as output:
        started = time.monotonic()
        deadline = started + STARTUP_SECONDS
        process = subprocess.Popen([*command, "--verbose", "--bind", HOST, "--port", str(port)],
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


def measured_round(number, command, cameras, directory, camera_timeout, sync_timeout):
    """Start a fresh dual-board example and time its camera and synchronization milestones."""
    port = local_port()
    log_path = directory / f"{number:03}.log"
    jpeg_ms = {unit: None for unit in cameras}
    both_jpegs_ms = sync_ready_ms = None
    status = {}
    started_utc = datetime.now(timezone.utc).isoformat()
    with log_path.open("xb") as log:
        started = time.monotonic()
        deadline = started + camera_timeout
        process = subprocess.Popen([*command, "--verbose", "--bind", HOST, "--port", str(port)],
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while process.poll() is None and time.monotonic() < deadline:
                for unit, observed in jpeg_ms.items():
                    if observed is None and has_jpeg(port, unit):
                        jpeg_ms[unit] = round((time.monotonic() - started) * 1000, 1)
                if both_jpegs_ms is None and all(t is not None for t in jpeg_ms.values()):
                    both_jpegs_ms = max(jpeg_ms.values())
                    deadline = started + both_jpegs_ms / 1000 + sync_timeout
                if both_jpegs_ms is not None:
                    body = fetch(port, "/status")
                    if body:
                        status = json.loads(body)
                        if status.get("fault"):
                            break
                        if status.get("ready") is True:
                            sync_ready_ms = round((time.monotonic() - started) * 1000, 1)
                            break
                time.sleep(0.05)
            exited = process.poll() is not None
        finally:
            clean_stop = stop(process)

    events = []
    bad_events = frontend_timeouts = 0
    with log_path.open(errors="replace") as log:
        for line in log:
            frontend_timeouts += line.count("Camera frontend has timed out")
            _, marker, payload = line.partition("B-CamSync startup ")
            if marker:
                try:
                    event, _ = json.JSONDecoder().raw_decode(payload.lstrip())
                    if event["unit"] not in cameras or event["outcome"] not in {"ready", "retry", "exhausted", "error"}:
                        raise ValueError("unexpected unit or startup outcome")
                    events.append(event)
                except (KeyError, TypeError, ValueError):
                    bad_events += 1
    if both_jpegs_ms is None:
        outcome = "camera_exit" if exited else "camera_timeout"
    elif sync_ready_ms is None:
        outcome = "sync_fault" if status.get("fault") else ("sync_exit" if exited else "sync_timeout")
    elif not clean_stop:
        outcome = "shutdown_failed"
    elif bad_events or {event["unit"] for event in events if event["outcome"] == "ready"} != set(cameras):
        outcome = "telemetry_incomplete"
    else:
        outcome = "ready"
    return {"round": number, "started_utc": started_utc, "outcome": outcome, "cameras": cameras,
            "jpeg_ms": jpeg_ms, "both_jpegs_ms": both_jpegs_ms, "sync_ready_ms": sync_ready_ms,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "status": status, "events": events, "frontend_timeouts": frontend_timeouts,
            "bad_events": bad_events, "clean_stop": clean_stop, "returncode": process.returncode,
            "log": log_path.name}


def measure(args):
    args.output_dir.mkdir(parents=True, exist_ok=True)
    cameras = {"board-1": args.camera_ids[0], "board-2": args.camera_ids[1]}
    command = [sys.executable, "-u", str(EXAMPLES / "opencv_sync_http.py"), *args.camera_ids]
    passed = 0
    with (args.output_dir / "rounds.jsonl").open("x") as output:
        for number in range(1, args.rounds + 1):
            record = measured_round(number, command, cameras, args.output_dir,
                                    args.camera_timeout, args.sync_timeout)
            output.write(json.dumps(record, allow_nan=False) + "\n")
            output.flush()
            passed += record["outcome"] == "ready"
            retries = sum(event["outcome"] == "retry" for event in record["events"])
            print(f"[{number}/{args.rounds}] {record['outcome']} retries={retries} "
                  f"both_jpegs_ms={record['both_jpegs_ms']} sync_ready_ms={record['sync_ready_ms']} "
                  f"passed={passed}/{number}", flush=True)
            if number < args.rounds:
                time.sleep(PAUSE_SECONDS)
    return int(passed != args.rounds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera-ids", nargs=2, metavar=("REFERENCE", "SECOND"))
    parser.add_argument("--output-dir", type=Path, help="write per-round logs and rounds.jsonl")
    parser.add_argument("--rounds", type=int, help="number of fresh dual-board starts")
    parser.add_argument("--camera-timeout", type=int, default=100, help="seconds allowed to obtain both previews")
    parser.add_argument("--sync-timeout", type=int, default=30, help="seconds for sync readiness after both previews")
    args = parser.parse_args()
    if args.camera_ids is not None or args.output_dir is not None or args.rounds is not None:
        if not args.camera_ids or not args.output_dir or not args.rounds or args.rounds < 1:
            parser.error("measurement requires --camera-ids, --output-dir and a positive --rounds")
        if args.camera_ids[0] == args.camera_ids[1] or min(args.camera_timeout, args.sync_timeout) <= 0:
            parser.error("measurement requires two distinct cameras and positive timeouts")
        return measure(args)
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
