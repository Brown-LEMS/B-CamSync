"""Consume local board packets through B-CamSync's public Python interface."""

import argparse
import math
from pathlib import Path
import time

from bcamsync import Config, Session


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("node")
    parser.add_argument("--seconds", type=float, default=10)
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds must be finite and positive")
    config = Config.from_file(args.config)
    units = [unit for unit in config.units if unit.node_id == args.node]
    coordinator = config.reference_node() == args.node
    with Session(config, args.node) as session:
        if not session.wait_ready(config.startup_timeout_seconds + 10):
            raise RuntimeError("Synchronization readiness timed out")
        print("Synchronization ready; consuming local frames", flush=True)
        end = time.monotonic() + args.seconds
        shutdown_deadline = end + config.startup_timeout_seconds + 10
        next_report = 0.0
        while True:
            status = session.status()
            if status.fault:
                raise RuntimeError(status.fault)
            if status.state == "stopped":
                if coordinator:
                    raise RuntimeError("Coordinator stopped unexpectedly")
                break
            if not status.ready:
                raise RuntimeError("Synchronization readiness was lost")
            if coordinator and time.monotonic() >= end:
                break
            if not coordinator and time.monotonic() >= shutdown_deadline:
                raise RuntimeError("Coordinator shutdown timed out")
            for unit in units:
                try:
                    packet = session.read(unit.id, timeout_seconds=1)
                except RuntimeError:
                    status = session.status()
                    if not coordinator and status.state == "stopped" and not status.fault:
                        break
                    raise
                if packet is None:
                    continue
                # Each readonly array has board stride and keeps its memory alive.
                views = packet.views
                if time.monotonic() >= next_report:
                    print(f"{packet.unit_id}: sequence={packet.sequence}, "
                          f"views={[view.shape for view in views]}, dropped={packet.dropped_before}", flush=True)
                    next_report = time.monotonic() + 1
    closed = session.status()
    if closed.fault or closed.state != "stopped" or closed.ready:
        raise RuntimeError(closed.fault or "Session did not close cleanly")


if __name__ == "__main__":
    main()
