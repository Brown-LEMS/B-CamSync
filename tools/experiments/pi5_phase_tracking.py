"""Offline baseline identification and feedback model used to reproduce the earlier Pi trials."""

import csv
from collections import defaultdict
import math
from statistics import mean


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
