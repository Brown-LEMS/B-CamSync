"""Operator-run device workflow validation through B-CamSync's public API.

Run on every configured node. Results describe delivered frames and timestamp
control measurements; optical alignment is measured separately by the operator.
"""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import platform
import sys
import time

import numpy as np

from bcamsync import Config, Session


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def status_record(status) -> dict:
    return {
        "state": status.state, "ready": status.ready, "fault": status.fault,
        "units": [{"unit_id": unit.unit_id, "state": unit.state, "ready": unit.ready,
                   "phase_ms": unit.phase_ms, "p95_ms": unit.p95_ms, "max_ms": unit.max_ms,
                   "measurement_end_ns": unit.measurement_end_ns,
                   "fault": unit.fault} for unit in sorted(status.units, key=lambda unit: unit.unit_id)],
    }


def retained_unchanged(retained: dict) -> None:
    for unit_id, (views, originals) in retained.items():
        for index, (view, original) in enumerate(zip(views, originals)):
            if not np.array_equal(view, original):
                raise RuntimeError(f"Retained frame changed: {unit_id} view {index}")


def graceful_peer_stop(session, coordinator: bool) -> bool:
    """Recognize coordinator-requested termination on a follower only."""
    state = session.status()
    if state.fault:
        raise RuntimeError(state.fault)
    if state.state == "stopped":
        if coordinator:
            raise RuntimeError("Coordinator stopped before completing its acquisition window")
        return True
    if not state.ready:
        raise RuntimeError("Synchronization readiness was lost")
    return False


def wait_coordinator_stop(session, retained: dict, timeout: float, run: int) -> None:
    """Keep the follower active until the coordinator closes the group."""
    progress(f"Run {run}: local measurement complete; waiting for coordinator shutdown")
    deadline = time.monotonic() + timeout
    next_report = 0.0
    while not graceful_peer_stop(session, coordinator=False):
        now = time.monotonic()
        if now >= deadline:
            raise RuntimeError("Coordinator did not stop within the shutdown wait deadline")
        if now >= next_report:
            retained_unchanged(retained)
            progress(f"Run {run}: waiting for coordinator; {max(0, deadline - now):.1f}s remaining")
            next_report = now + 5
        time.sleep(min(0.1, deadline - now))


def validate_run(config, node: str, duration: float, run: int, report: dict) -> None:
    units = {unit.id: unit for unit in config.units if unit.node_id == node}
    if not units:
        raise ValueError(f"Node {node} has no local units")
    stats = {unit_id: {"delivered_packets": 0, "sequence_gaps": 0, "dropped_before": 0,
                       "first_sequence": None, "last_sequence": None,
                       "first_timestamp_ns": None, "last_timestamp_ns": None,
                       "minimum_observed_period_ms": None, "maximum_observed_period_ms": None}
             for unit_id in sorted(units)}
    report.update({"run": run, "passed": False, "boards": stats, "status_samples": []})
    retained = {}
    coordinator = config.reference_node() == node
    coordinator_stopped = False
    shutdown_error = None
    started = time.monotonic()
    session = Session(config, node)
    try:
        progress(f"Run {run}: starting {len(units)} local boards")
        session.start()
        deadline = time.monotonic() + config.startup_timeout_seconds + 10
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Synchronization readiness timed out")
            if session.wait_ready(min(5, remaining)):
                break
            state = session.status()
            if state.state == "stopped":
                raise RuntimeError("Session stopped before synchronization readiness")
            progress(f"Run {run}: {state.state}; {time.monotonic() - started:.1f}s since start")
        report["startup_seconds"] = time.monotonic() - started
        progress(f"Run {run}: synchronization ready in {report['startup_seconds']:.2f}s")
        began = time.monotonic()
        next_report = began
        while time.monotonic() - began < duration:
            if graceful_peer_stop(session, coordinator):
                coordinator_stopped = True
                break
            for unit_id, unit in units.items():
                try:
                    packet = session.read(unit_id, timeout_seconds=1)
                except RuntimeError:
                    if graceful_peer_stop(session, coordinator):
                        coordinator_stopped = True
                        break
                    raise
                if packet is None:
                    continue
                if packet.unit_id != unit_id:
                    raise RuntimeError(f"Wrong packet identity for {unit_id}")
                views = packet.views
                if len(views) != unit.views:
                    raise RuntimeError(f"Wrong camera view count for {unit_id}")
                for view in views:
                    if view.shape != (unit.height, unit.width // unit.views) or view.dtype != np.uint8:
                        raise RuntimeError(f"Wrong view shape or pixel type for {unit_id}")
                    if view.flags.writeable or view.strides != (unit.width, 1):
                        raise RuntimeError(f"Wrong view ownership or stride for {unit_id}")
                current = stats[unit_id]
                if current["last_sequence"] is not None:
                    gap = packet.sequence - current["last_sequence"]
                    elapsed = packet.timestamp_ns - current["last_timestamp_ns"]
                    if gap <= 0 or elapsed <= 0:
                        raise RuntimeError(f"Non-increasing frame sequence or timestamp for {unit_id}")
                    current["sequence_gaps"] += gap - 1
                    period = elapsed / gap / 1e6
                    for name, select in (("minimum_observed_period_ms", min), ("maximum_observed_period_ms", max)):
                        current[name] = period if current[name] is None else select(current[name], period)
                else:
                    current["first_sequence"] = packet.sequence
                    current["first_timestamp_ns"] = packet.timestamp_ns
                    retained[unit_id] = (views, tuple(view.copy() for view in views))
                current["delivered_packets"] += 1
                current["last_sequence"] = packet.sequence
                current["last_timestamp_ns"] = packet.timestamp_ns
                current["dropped_before"] = max(current["dropped_before"], packet.dropped_before)
            if coordinator_stopped:
                break
            now = time.monotonic()
            if now >= next_report:
                state = session.status()
                if graceful_peer_stop(session, coordinator):
                    coordinator_stopped = True
                    break
                sample = status_record(state)
                sample["elapsed_seconds"] = now - began
                report["status_samples"].append(sample)
                retained_unchanged(retained)
                counts = ", ".join(f"{unit_id}={value['delivered_packets']}" for unit_id, value in stats.items())
                progress(f"Run {run}: {min(now - began, duration):.1f}/{duration:.1f}s; packets {counts}")
                next_report = now + 5
        report["acquisition_seconds"] = time.monotonic() - began
        for unit_id, value in stats.items():
            if value["delivered_packets"] < 2:
                raise RuntimeError(f"Insufficient frames for {unit_id}")
            value["delivered_fps"] = value["delivered_packets"] / report["acquisition_seconds"]
            value["observed_capture_fps"] = ((value["last_sequence"] - value["first_sequence"]) * 1e9 /
                                             (value["last_timestamp_ns"] - value["first_timestamp_ns"]))
        report["final_status"] = status_record(session.status())
        if not coordinator and not coordinator_stopped:
            waiting = time.monotonic()
            wait_coordinator_stop(session, retained, config.startup_timeout_seconds + 10, run)
            report["coordinator_shutdown_wait_seconds"] = time.monotonic() - waiting
        if not coordinator:
            report["coordinator_shutdown_observed"] = True
    finally:
        try:
            session.stop()
        except RuntimeError as error:
            shutdown_error = str(error)
            report["shutdown_error"] = shutdown_error
        report["shutdown_status"] = status_record(session.status())
    closed = report["shutdown_status"]
    if shutdown_error or closed["fault"] or closed["state"] != "stopped" or closed["ready"]:
        raise RuntimeError(shutdown_error or closed["fault"] or "Session did not close cleanly")
    # The application may retain NumPy views after both session and packet die.
    del packet
    del session
    retained_unchanged(retained)
    report["retained_storage_check"] = "passed_after_capture_and_session_shutdown"
    report["passed"] = True
    progress(f"Run {run}: completed and closed cleanly")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("node")
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0 or args.repeat < 1:
        parser.error("--seconds must be finite and positive; --repeat must be positive")
    result = {"group_id": None, "local_node": args.node,
              "started_utc": datetime.now(timezone.utc).isoformat(),
              "platform": platform.platform(), "requested_seconds_per_run": args.seconds,
              "requested_runs": args.repeat, "passed": False, "runs": [],
              "measurement_kind": "local_frame_delivery_and_timestamp_control",
              "optical_validation": "operator_measurement_not_performed_by_this_tool"}
    try:
        config = Config.from_file(args.config)
        result["group_id"] = config.group_id
        result["backend"] = config.backend
        if config.backend == "fake":
            result["measurement_kind"] = "software_fake_frame_delivery_and_control"
        result["reference_unit"] = config.reference_unit
        for index in range(1, args.repeat + 1):
            report = {}
            result["runs"].append(report)
            validate_run(config, args.node, args.seconds, index, report)
        result["passed"] = True
    except (RuntimeError, ValueError, OSError, KeyboardInterrupt) as error:
        result["error"] = str(error) or "Interrupted by operator"
        progress(f"Validation ended: {result['error']}")
    rendered = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(rendered)
        progress(f"Results saved to {args.output}")
    print(rendered, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
