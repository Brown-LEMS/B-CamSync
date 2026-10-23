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
    with Session(config, args.node) as session:
        print("Consuming local frames; synchronization status is reported separately", flush=True)
        end = time.monotonic() + args.seconds
        next_report = 0.0
        while time.monotonic() < end:
            status = session.status()
            for unit in units:
                packet = session.read(unit.id, timeout_seconds=1)
                if packet is None:
                    continue
                # Each readonly array has board stride and keeps its memory alive.
                views = packet.views
                if time.monotonic() >= next_report:
                    print(f"{packet.unit_id}: sequence={packet.sequence}, "
                          f"views={[view.shape for view in views]}, dropped={packet.dropped_before}, "
                          f"clock_valid={packet.clock_valid}, sync={status.state}, fault={status.fault}", flush=True)
                    next_report = time.monotonic() + 1
    closed = session.status()
    if closed.fault:
        print(f"Synchronization fault: {closed.fault}", flush=True)


if __name__ == "__main__":
    main()
