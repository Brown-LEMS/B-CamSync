"""Compare the stock controller to calibrated rate/phase control after stock initial lock."""

import argparse
import csv
from collections import defaultdict
import math
import os
from pathlib import Path
from statistics import mean
import time


def identify(path, nominal):
    """Use the settled interior of two repeated request sweeps."""
    intervals = defaultdict(list)
    previous = {}
    with path.open() as stream:
        for row in csv.DictReader(stream):
            unit, stage = row["unit"], row["stage"]
            t, seq = int(row["sensor_ns"]), int(row["sequence"])
            if unit in previous:
                last, n = previous[unit]
                if seq != n + 1:
                    raise ValueError("Calibration input contains a sequence gap")
                intervals[unit, stage].append((t - last) / 1000)
            previous[unit] = t, seq
            if stage == "baseline" and int(row["requested_us"]) != nominal:
                raise ValueError("Calibration was captured at a different rate")
    reference, base = [mean(intervals[u, "baseline"][5:-3]) for u in ("board-1", "board-2")]
    shifts = {}
    for unit, stage in intervals:
        if unit == "board-2" and stage.startswith("step1_"):
            delta = int(stage.split("_")[1])
            measured = [mean(intervals[unit, f"step{i}_{delta:+d}"][5:-3]) - base for i in (1, 2)]
            # Select reproducible, signed actions separated from baseline noise in both repeats.
            if all(x * math.copysign(1, delta) > 1 for x in measured):
                shifts[delta] = mean(measured)
    smaller = [d for d in shifts if d < 0]
    larger = [d for d in shifts if d > 0]
    if not smaller or not larger:
        raise ValueError("Sweep did not identify achievable periods on both sides of nominal")
    minus, plus = max(smaller), min(larger)
    return reference, base - reference, (minus, shifts[minus]), (plus, shifts[plus])


class Tracker:
    """Accumulate sub-step desired movement and dispatch calibrated one-frame requests."""

    def __init__(self, probes, model, fps):
        self.reference, self.follower = probes
        self.period, self.drift, self.negative, self.positive = model
        self.horizon = fps  # One-second proportional settling time.
        self.debt = 0

    def request(self):
        delta = (self.follower.latest - self.reference.latest) / 1000
        # Nearest-cycle signed phase. Both latest timestamps may arrive in one batch.
        phase = math.remainder(delta, self.period)
        desired = -self.drift - phase / self.horizon
        # An unachievable movement must not accumulate as debt after a disturbance.
        self.debt += max(self.negative[1], min(self.positive[1], desired))
        command, shift = self.positive if self.debt >= 0 else self.negative
        if abs(self.debt) >= abs(shift) / 2:
            self.debt -= shift
            return self.follower.baseline_us + command
        return self.follower.baseline_us


def run(args):
    from bcamsync import SyncSession
    from pi5_phase_response import capture_pair, check_progress

    model = identify(args.calibration, 1_000_000 // args.fps)
    print(f"{args.fps} Hz calibration (reference_us, drift_us/frame, command/response_us): {model}", flush=True)
    with capture_pair(args.fps, args.output) as (config, capture, probes):
        for probe in probes:
            probe.stage = "initial_lock"
        session = SyncSession(config, config.nodes[0].id, capture.units)
        session.start()
        try:
            if not session.wait_ready(90):
                raise RuntimeError("No initial lock before measurement")
            print("Stock initial lock complete", flush=True)
            if args.mode == "adaptive":
                session.stop()
                print("Stock synchronization stopped; capture continues under the experimental controller", flush=True)
                probes[1].control = Tracker(probes, model, args.fps).request
            for probe in probes:
                probe.stage = args.mode
            start = time.monotonic()
            while time.monotonic() - start < args.seconds:
                time.sleep(min(5, max(0, args.seconds - (time.monotonic() - start))))
                check_progress(capture, probes)
                if args.mode == "stock" and not session.status().ready:
                    raise RuntimeError(f"Stock tracking lost readiness: {session.status().fault}")
                print(f"{args.mode}: {time.monotonic() - start:.1f}/{args.seconds}s", flush=True)
        finally:
            session.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fps", type=int, required=True)
    parser.add_argument("--seconds", type=int, required=True)
    parser.add_argument("--mode", choices=["stock", "adaptive"], required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "*:ERROR")
    args = parser.parse_args()
    if args.fps <= 0 or args.seconds <= 0:
        parser.error("--fps and --seconds must be positive")
    run(args)
