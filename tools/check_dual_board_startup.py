"""Measure ten fresh dual-board camera startups on a Pi."""

from _camera_startup import main


if __name__ == "__main__":
    raise SystemExit(main("opencv_sync_http.py", 2))
