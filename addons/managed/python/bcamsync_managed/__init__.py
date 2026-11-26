"""Optional C++ camera acquisition built on the B-CamSync synchronization core."""

from contextlib import contextmanager

from ._native import (
    Config, FakeOptions, FramePacket, Session, UnitConfig, configure_camera_console, enumerate_arducam_cameras,
)

__all__ = [
    "Config", "FakeOptions", "FramePacket", "Session", "UnitConfig", "camera_cli_output", "configure_camera_console",
    "enumerate_arducam_cameras",
]


@contextmanager
def camera_cli_output(verbose):
    """Configure camera CLI diagnostics and report terminal capture errors."""
    configure_camera_console(verbose)
    try:
        yield
    except (ValueError, OSError, RuntimeError) as error:
        if verbose:
            raise
        raise SystemExit(f"B-CamSync: {error}") from None
