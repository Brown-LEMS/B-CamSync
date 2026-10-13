#!/usr/bin/env python3
"""Run B-CamSync node provisioning from a source checkout or installed package."""

from pathlib import Path
import runpy

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "python" / "bcamsync" / "platform.py"), run_name="__main__")
