"""Optional C++ camera acquisition built on the B-CamSync synchronization core."""

from ._native import (
    Config, FakeOptions, FramePacket, Session, UnitConfig, enumerate_arducam_cameras,
)

__all__ = [
    "Config", "FakeOptions", "FramePacket", "Session", "UnitConfig",
    "enumerate_arducam_cameras",
]
