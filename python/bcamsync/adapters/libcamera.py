"""Reusable metadata hooks for libcamera; importing this module loads no camera SDK."""

from collections import deque
import threading

from . import (
    CorrectionStatus, FrameTiming, PhaseActuator, Pulse, Submission, SyncUnit,
    TimingSnapshot, boot_time_ns, monotonic_ns,
)

__all__ = ["CameraTiming"]


class CameraTiming(SyncUnit):
    """Timing/control adapter for application-owned Linux libcamera capture.

    Construct with the unit ID, group frame rate, and actual SDK duration limits
    in microseconds. This object never opens cameras or owns pixels or threads.
    Before queueing a request, apply next_submission().duration_us through
    FrameDurationLimits and retain the returned Submission with that request.
    On completion, call observe() with the sensor timestamp (CLOCK_BOOTTIME),
    camera sequence, reported FrameDuration, and the retained Submission.

    The adapter keeps 240 recent observations. Restoration uses the reported
    pre-pulse period, including effects delayed beyond the restoration request.
    """
    def __init__(self, unit_id: str, fps: int, minimum_us: int, maximum_us: int):
        super().__init__()
        self.unit_id = unit_id
        self.baseline_us = 1_000_000 // fps
        self.minimum_us, self.maximum_us = minimum_us, maximum_us
        self.actuator = PhaseActuator()
        self.frames = deque(maxlen=240)
        self.lock = threading.Lock()
        self.binding = None
        self.error = ""
        self.corrections = 0

    def bind(self, session_id: str, round_id: int) -> None:
        with self.lock:
            self.actuator.bind(session_id, round_id)
            self.binding = session_id, round_id

    def next_submission(self) -> Submission:
        """Capture callback: get the period to apply and retain its submission token."""
        with self.lock:
            return self.actuator.next(self.baseline_us, monotonic_ns())

    def observe(self, sequence: int, sensor_ns: int, reported_us: int, submitted: Submission) -> None:
        """Capture callback: report actual metadata without modifying sensor time."""
        with self.lock:
            frame = FrameTiming()
            frame.camera_sequence = sequence
            frame.sensor_timestamp_ns = frame.timestamp_ns = sensor_ns
            frame.requested_duration_us = submitted.duration_us
            frame.reported_duration_us = reported_us
            frame.correction_id = submitted.correction_id
            self.frames.append(frame)
            self.actuator.observe_duration(submitted, sequence, sensor_ns, reported_us)

    def snapshot(self) -> TimingSnapshot:
        return self.snapshot_from(None)

    def snapshot_from(self, sequence) -> TimingSnapshot:
        """Return available observations starting at the inclusive sequence cursor."""
        with self.lock:
            result = TimingSnapshot()
            result.unit_id = self.unit_id
            result.minimum_duration_us = self.minimum_us
            result.maximum_duration_us = self.maximum_us
            result.snapshot_timestamp_ns = boot_time_ns()
            if sequence is None:
                result.frames = list(self.frames)
            else:
                rows = []
                for frame in reversed(self.frames):
                    rows.append(frame)
                    if frame.camera_sequence == sequence:
                        break
                result.frames = rows[::-1]
            result.correction = self.actuator.snapshot()
            return result

    def adjust(self, pulse: Pulse) -> CorrectionStatus:
        """SyncSession callback: accept a correction chosen by the library."""
        with self.lock:
            if not self.frames:
                raise RuntimeError("A completed baseline frame is required before adjusting the camera period")
            result = self.actuator.adjust(*self.binding, pulse, boot_time_ns(), monotonic_ns(),
                                          self.baseline_us, self.minimum_us, self.maximum_us)
            self.corrections += 1
            return result

    def cancel(self) -> None:
        with self.lock:
            self.actuator.cancel(*self.binding)

    def fault(self) -> str:
        return self.error
