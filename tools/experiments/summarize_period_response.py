"""Summarize stable sensor intervals in a pi5_phase_response.py capture."""

import argparse
import csv
from itertools import groupby
import json
from pathlib import Path
from statistics import median


def summarize(path, settle_frames):
    by_unit = {}
    with path.open(newline="") as stream:
        for row in csv.DictReader(stream):
            by_unit.setdefault(row["unit"], []).append(row)
    if len(by_unit) != 2:
        raise ValueError(f"Expected two captured units, found {len(by_unit)}")
    summaries = {}
    for unit, rows in by_unit.items():
        stages = {}
        gaps = 0
        for previous, current in zip(rows, rows[1:]):
            advance = (int(current["sequence"]) - int(previous["sequence"])) % (1 << 32)
            if not 0 < advance < (1 << 31):
                raise ValueError(f"{unit}: repeated or reversed frame sequence")
            gaps += advance - 1
        for stage, group in groupby(rows, key=lambda row: row["stage"]):
            if stage == "startup":
                continue
            frames = list(group)[settle_frames:]
            if len(frames) < 2:
                raise ValueError(f"{unit}/{stage}: too few settled frames")
            periods = [(int(right["sensor_ns"]) - int(left["sensor_ns"])) / 1000
                       for left, right in zip(frames, frames[1:])
                       if (int(right["sequence"]) - int(left["sequence"])) % (1 << 32) == 1]
            if not periods or min(periods) <= 0:
                raise ValueError(f"{unit}/{stage}: invalid or missing contiguous timestamps")
            stages[stage] = {
                "intervals": len(periods), "requested_us": int(frames[0]["requested_us"]),
                "reported_median_us": median(int(frame["reported_us"]) for frame in frames),
                "measured_mean_us": round(sum(periods) / len(periods), 4),
            }
        if "baseline" not in stages or "finish" not in stages:
            raise ValueError(f"{unit}: incomplete period sweep")
        summaries[unit] = {"sequence_gaps": gaps, "stages": stages}
    return summaries


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--settle-frames", type=int, default=8,
                        help="Skip this many completed requests in each new stage")
    args = parser.parse_args()
    if args.settle_frames < 0:
        parser.error("--settle-frames must be nonnegative")
    print(json.dumps(summarize(args.csv, args.settle_frames), indent=2))
