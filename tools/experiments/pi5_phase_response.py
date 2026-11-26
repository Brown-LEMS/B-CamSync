"""Measure per-request Pi 5 B0267 frame-period responses using only camera metadata."""

import argparse
import csv
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "examples"))
from opencv_sync import Capture, sync_config


@dataclass(frozen=True)
class Command:
    duration_us: int
    stage: str
    queued_ns: int
    source: object


class ProbeTiming:
    """Record actual request/completion correspondence while retaining the SDK adapter."""

    def __init__(self, timing, writer):
        self.adapter = timing
        self.unit_id = timing.unit_id
        self.baseline_us = timing.baseline_us
        self.minimum_us = timing.minimum_us
        self.maximum_us = timing.maximum_us
        self.stage = "startup"
        self.control = None
        self.writer = writer
        self.latest = None

    def next_submission(self):
        source = self.adapter.next_submission()
        period = source.duration_us if self.control is None else self.control()
        if not self.minimum_us <= period <= self.maximum_us:
            raise ValueError(f"Requested {period} us exceeds configured limits")
        return Command(period, self.stage, time.clock_gettime_ns(time.CLOCK_BOOTTIME), source)

    def observe(self, sequence, sensor_ns, reported_us, submitted):
        self.latest = sensor_ns
        self.writer.writerow((self.unit_id, sequence, sensor_ns, reported_us, submitted.duration_us,
                              submitted.stage, submitted.queued_ns, time.clock_gettime_ns(time.CLOCK_BOOTTIME),
                              submitted.source.correction_id))
        self.adapter.observe(sequence, sensor_ns, reported_us, submitted.source)


def schedule(fps):
    yield "baseline", 0, 8 * fps
    # Same signed steps at different phases of the boards' natural drift.
    for repeat in (1, 2):
        for delta in (1, -1, 2, -2, 4, -4, 8, -8, 16, -16, 32, -32, 100, -100):
            yield f"step{repeat}_{delta:+d}", delta, 24
        yield f"rest{repeat}", 0, fps * 2
    yield "finish", 0, fps * 4


@contextmanager
def capture_pair(fps, output):
    import libcamera
    manager = libcamera.CameraManager.singleton()
    ids = sorted(camera.id for camera in manager.cameras)
    if len(ids) != 2:
        raise RuntimeError(f"Expected two cameras, found {ids}")
    config = sync_config()
    config.fps = fps
    config.validate()
    with output.open("x", newline="") as stream, ExitStack() as stack:
        writer = csv.writer(stream)
        writer.writerow(("unit", "sequence", "sensor_ns", "reported_us", "requested_us", "stage",
                         "queued_ns", "received_ns", "correction_id"))
        capture = Capture(ids, config, stack, verbose=True)
        stack.callback(capture.close)
        probes = []
        for board in capture.cameras:
            board.timing = ProbeTiming(board.timing, writer)
            if not board.timing.minimum_us <= board.timing.baseline_us <= board.timing.maximum_us:
                raise ValueError(f"Requested {board.timing.baseline_us} us outside device limits "
                                 f"{board.timing.minimum_us}..{board.timing.maximum_us}")
            probes.append(board.timing)
            print(f"{board.timing.unit_id} {board.camera.id} limits_us={board.timing.minimum_us}.."
                  f"{board.timing.maximum_us} nominal={board.timing.baseline_us}", flush=True)
        capture.start(None)
        yield config, capture, probes


def check_progress(capture, probes):
    if capture.error:
        raise RuntimeError("Capture loop failed") from capture.error
    now = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    if any(probe.latest is None or now - probe.latest > 2_000_000_000 for probe in probes):
        raise RuntimeError("No fresh sensor timestamp from both cameras")


def measure(fps, output):
    with capture_pair(fps, output) as (_, capture, probes):
        for stage, delta, frames in schedule(fps):
            print(f"fps={fps} stage={stage} delta_us={delta} planned_seconds={frames / fps:.2f}", flush=True)
            for i, probe in enumerate(probes):
                period = probe.baseline_us + (delta if i else 0)
                probe.stage = stage
                probe.control = lambda period=period: period
            time.sleep(frames / fps)
            check_progress(capture, probes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fps", required=True, type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.fps <= 0:
        parser.error("--fps must be positive")
    os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "*:ERROR")
    measure(args.fps, args.output)


if __name__ == "__main__":
    main()
