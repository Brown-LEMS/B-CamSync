"""Device adapter interfaces called by SyncSession and capture callbacks.

Applications normally use SyncConfig, SyncSession, and status objects from
bcamsync. Adapter implementers use these types to report timing and execute
library-generated corrections; they do not choose synchronization corrections.
"""

from .._native import (
    CorrectionStatus, FrameTiming, PhaseActuator, Pulse, Submission, SyncUnit,
    TimingSnapshot, boot_time_ns, monotonic_ns,
)

__all__ = [
    "CorrectionStatus", "FrameTiming", "PhaseActuator", "Pulse", "Submission",
    "SyncUnit", "TimingSnapshot", "boot_time_ns", "monotonic_ns",
]
