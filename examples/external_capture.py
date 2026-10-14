"""Synchronize application-owned synthetic capture through the public adapter API."""

import argparse
from collections import deque
import threading

from bcamsync import (
    FrameTiming, NodeConfig, PhaseActuator, SyncConfig, SyncSession, SyncUnit,
    SyncUnitConfig, TimingSnapshot, boot_time_ns, monotonic_ns,
)


class SyntheticCapture(SyncUnit):
    """A user-owned producer; replace its duration submission with your camera SDK calls."""

    def __init__(self, unit_id, phase_ns=0, fps=40):
        super().__init__()
        self.unit_id = unit_id
        self.phase_ns = phase_ns
        self.baseline_us = 1_000_000 // fps
        self.sequence = 0
        self.actuator = PhaseActuator()
        self.binding = None
        self.frames = deque(maxlen=240)
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.thread = None
        self.capture_fault = ""

    def start_capture(self):
        self.thread = threading.Thread(target=self._capture, name=self.unit_id)
        self.thread.start()

    def stop_capture(self):
        self.closed.set()
        if self.thread is not None:
            self.thread.join()

    def _capture(self):
        period_ns = self.baseline_us * 1000
        now = boot_time_ns()
        next_ns = now - now % period_ns + period_ns + self.phase_ns
        submitted = self.actuator.next(self.baseline_us, monotonic_ns())
        try:
            while not self.closed.wait(max(0, next_ns - boot_time_ns()) / 1e9):
                with self.lock:
                    self.sequence += 1
                    frame = FrameTiming()
                    frame.camera_sequence = self.sequence
                    frame.sensor_timestamp_ns = frame.timestamp_ns = next_ns
                    frame.requested_duration_us = frame.reported_duration_us = submitted.duration_us
                    frame.correction_id = submitted.correction_id
                    self.frames.append(frame)
                    # In a camera adapter this acknowledgement comes from observed
                    # request completion, after the baseline duration took effect.
                    self.actuator.completed(submitted, self.sequence, next_ns)
                    submitted = self.actuator.next(self.baseline_us, monotonic_ns())
                    next_ns += submitted.duration_us * 1000
        except Exception as error:
            self.capture_fault = str(error)

    def bind(self, session_id, round_id):
        with self.lock:
            if self.binding is not None and self.binding[0] != session_id:
                if self.actuator.snapshot().state not in {"idle", "restored"}:
                    raise RuntimeError("Prior synchronization correction is still active")
                self.actuator = PhaseActuator()
            self.actuator.bind(session_id, round_id)
            self.binding = (session_id, round_id)

    def snapshot(self):
        with self.lock:
            snapshot = TimingSnapshot()
            snapshot.unit_id = self.unit_id
            snapshot.snapshot_timestamp_ns = boot_time_ns()
            snapshot.frames = list(self.frames)
            snapshot.correction = self.actuator.snapshot()
            return snapshot

    def adjust(self, pulse):
        with self.lock:
            return self.actuator.adjust(*self.binding, pulse, boot_time_ns(), monotonic_ns(),
                                        self.baseline_us, self.baseline_us - 1000,
                                        self.baseline_us + 1000)

    def cancel(self):
        with self.lock:
            self.actuator.cancel(*self.binding)

    def fault(self):
        return self.capture_fault


def local_config():
    config = SyncConfig()
    config.group_id = "external-capture"
    config.reference_unit = "reference"
    node = NodeConfig()
    node.id = "local"
    config.nodes = [node]
    units = []
    for unit_id in ("reference", "follower"):
        unit = SyncUnitConfig()
        unit.id, unit.node_id = unit_id, "local"
        units.append(unit)
    config.units = units
    config.startup_timeout_seconds = 30
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=25)
    args = parser.parse_args()
    captures = {"reference": SyntheticCapture("reference"),
                "follower": SyntheticCapture("follower", phase_ns=2_000_000)}
    for capture in captures.values():
        capture.start_capture()
    try:
        with SyncSession(local_config(), "local", captures) as synchronization:
            if not synchronization.wait_ready(args.timeout):
                raise RuntimeError("Synchronization readiness timed out")
            print(f"Synchronization ready: {synchronization.status().state}", flush=True)
            mapping = synchronization.clock()
            for capture in captures.values():
                timing = capture.snapshot().frames[-1]
                print(f"{capture.unit_id}: sequence={timing.camera_sequence}, "
                      f"group_timestamp_ns={mapping.map(timing.timestamp_ns)}", flush=True)
        print("Synchronization stopped; application capture is still running", flush=True)
    finally:
        for capture in captures.values():
            capture.stop_capture()


if __name__ == "__main__":
    main()
