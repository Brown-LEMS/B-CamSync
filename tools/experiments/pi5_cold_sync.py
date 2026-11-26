"""Measure cold acquisition and continuous tracking by the public SyncSession on two Pi cameras."""

import argparse
import json
import os
from pathlib import Path
import time


def report(status, start):
    print(json.dumps({
        "elapsed_s": round(time.monotonic() - start, 3),
        "state": status.state, "ready": status.ready, "fault": status.fault,
        "units": [{"unit": u.unit_id, "state": u.state, "phase_ms": u.phase_ms,
                   "p95_ms": u.p95_ms, "max_ms": u.max_ms} for u in status.units],
    }), flush=True)


def measure(session, probes, capture_health, seconds, started):
    for probe in probes:
        probe.stage = "acquisition"
    session_start = time.monotonic()
    session.start()
    try:
        next_report = session_start
        while not session.wait_ready(0.25):
            capture_health()
            status = session.status()
            if status.state == "stopped":
                raise RuntimeError("Synchronization stopped before initial readiness")
            if time.monotonic() >= next_report:
                report(status, started)
                next_report = time.monotonic() + 1
        capture_health()
        for probe in probes:
            probe.stage = "tracking"
        locked = time.monotonic()
        print(json.dumps({"cold_to_ready_s": locked - started,
                          "session_to_ready_s": locked - session_start}), flush=True)
        next_report = locked
        while True:
            capture_health()
            status = session.status()
            if not status.ready:
                report(status, started)
                raise RuntimeError(f"Lost synchronization readiness: {status.state} {status.fault}")
            now = time.monotonic()
            remaining = locked + seconds - now
            if now >= next_report or remaining <= 0:
                report(status, started)
                next_report = now + 5
            if remaining <= 0:
                break
            time.sleep(min(0.25, remaining))
    finally:
        for probe in probes:
            probe.stage = "restoring"
        session.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fps", type=int, required=True)
    parser.add_argument("--seconds", type=int, default=303, help="tracking time after the first lock")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.fps <= 0 or args.seconds <= 0:
        parser.error("--fps and --seconds must be positive")
    os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "*:ERROR")
    from bcamsync import SyncSession
    from pi5_phase_response import capture_pair, check_progress

    started = time.monotonic()
    with capture_pair(args.fps, args.output) as (config, capture, probes):
        print(f"Cold-start configuration: {config.to_json()}", flush=True)
        session = SyncSession(config, config.nodes[0].id, capture.units)
        measure(session, probes, lambda: check_progress(capture, probes), args.seconds, started)


if __name__ == "__main__":
    main()
