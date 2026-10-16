"""Camera-independent synchronization through application-owned SyncUnit adapters."""

from ._native import (
    ClockMapping, CorrectionStatus, FrameTiming, GroupStatus, NetworkSession,
    NodeConfig, PhaseActuator, PhasePolicy, Pulse, Submission, SyncConfig,
    SyncSession, SyncUnit, SyncUnitConfig, TimingSnapshot, UnitStatus,
    boot_time_ns, monotonic_ns,
)

__all__ = [
    "ClockMapping", "CorrectionStatus", "FrameTiming", "GroupStatus", "NetworkSession",
    "NodeConfig", "PhaseActuator", "PhasePolicy", "Pulse", "Submission", "SyncConfig",
    "SyncSession", "SyncUnit", "SyncUnitConfig", "TimingSnapshot", "UnitStatus",
    "boot_time_ns", "monotonic_ns",
]

_MANAGED_EXPORTS = {
    "Config", "FakeOptions", "FramePacket", "Session", "UnitConfig",
    "enumerate_arducam_cameras",
}


def __getattr__(name):
    """Keep explicit legacy managed imports compatible when the add-on is installed."""
    if name in _MANAGED_EXPORTS:
        import bcamsync_managed
        value = getattr(bcamsync_managed, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
