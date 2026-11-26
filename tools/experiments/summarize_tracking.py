"""Score paired Pi sensor timestamps without changing frame correspondence after lock."""

import argparse
import bisect
import csv
import json
import math
from pathlib import Path
from statistics import mean, median


def nearest_rank(values, fraction):
    return sorted(values)[math.ceil(len(values) * fraction) - 1]


def read_boards(path, mode):
    boards = {"board-1": [], "board-2": []}
    with path.open(newline="") as stream:
        for row in csv.DictReader(stream):
            if row["stage"] == mode:
                boards[row["unit"]].append(tuple(int(row[key]) for key in (
                    "sequence", "sensor_ns", "requested_us")))
    for unit, frames in boards.items():
        if len(frames) < 2:
            raise ValueError(f"{unit}: insufficient frames in {mode}")
        for prior, current in zip(frames, frames[1:]):
            if prior[0] >= current[0] or prior[1] >= current[1]:
                raise ValueError(f"{unit}: counter reset or nonmonotonic sensor time")
    return boards["board-1"], boards["board-2"]


def cadence(frames, start, stop, fps):
    timestamps = [t for _, t, _ in frames if start <= t < stop]
    intervals = [(b - a) / 1000 for a, b in zip(timestamps, timestamps[1:])]
    return {
        "intervals_over_1_5_periods": sum(dt > 1_500_000 / fps for dt in intervals),
        "max_interval_us": max(intervals, default=None),
    }


def score(path, mode, fps, skip_seconds, seconds):
    reference, follower = read_boards(path, mode)
    follower_times = [f[1] for f in follower]
    # Fix the sequence offset once at the start of the common capture interval.
    first = next((frame for frame in reference if frame[1] >= follower_times[0]), None)
    if first is None or first[1] > follower_times[-1]:
        raise ValueError("No common capture interval")
    index = bisect.bisect_left(follower_times, first[1])
    peer = min(follower[max(0, index-1):index+1], key=lambda frame: abs(frame[1] - first[1]))
    if abs(peer[1] - first[1]) >= 0.5e9 / fps:
        raise ValueError("Cannot establish initial frame correspondence")
    offset = peer[0] - first[0]
    follower_by_seq = {f[0]: f for f in follower}
    start = first[1] + round(skip_seconds * 1e9)
    stop = start + round(seconds * 1e9)
    if min(reference[-1][1], follower[-1][1]) < stop - 1e9 / fps:
        raise ValueError("Requested scoring interval exceeds trace")
    frames = [f for f in reference if start <= f[1] < stop]
    if len(frames) < 2:
        raise ValueError("Insufficient frames in the scoring interval")
    pairs = [(f, follower_by_seq[f[0]+offset]) for f in frames if f[0]+offset in follower_by_seq]
    if not pairs:
        raise ValueError("No frame pairs in the scoring interval")
    diffs = [(b[1] - a[1])/1000 for a,b in pairs]
    absolute = [abs(d) for d in diffs]
    return {
        "sequence_offset": offset, "pairs": len(pairs), "expected_by_sequence": frames[-1][0]-frames[0][0]+1,
        "signed_median_us": median(diffs), "mae_us": mean(absolute), "p95_us": nearest_rank(absolute, .95),
        "p99_us": nearest_rank(absolute, .99), "max_us": max(absolute),
        "follower_nonbaseline_requests_in_pairs": sum(b[2] != 1_000_000//fps for _, b in pairs),
        "reference_cadence": cadence(reference, start, stop, fps),
        "follower_cadence": cadence(follower, start, stop, fps),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--mode", choices=["stock", "adaptive", "centered", "tracking"], required=True)
    parser.add_argument("--fps", type=int, required=True)
    parser.add_argument("--skip-seconds", type=float, required=True)
    parser.add_argument("--seconds", type=float, required=True)
    args = parser.parse_args()
    if args.fps <= 0 or args.skip_seconds < 0 or args.seconds <= 0:
        parser.error("Require a positive rate/duration and a nonnegative skip")
    print(json.dumps(score(args.csv, args.mode, args.fps, args.skip_seconds, args.seconds), indent=2))
