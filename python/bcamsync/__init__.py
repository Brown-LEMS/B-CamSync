"""Synchronized local camera acquisition backed by the B-CamSync C++ library."""

from ._native import Config, FakeOptions, FramePacket, GroupStatus, NodeConfig, Session, UnitConfig, UnitStatus, enumerate_arducam_cameras

__all__ = ["Config", "FakeOptions", "FramePacket", "GroupStatus", "NodeConfig", "Session", "UnitConfig", "UnitStatus", "enumerate_arducam_cameras"]
