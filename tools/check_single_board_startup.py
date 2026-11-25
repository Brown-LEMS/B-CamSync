"""Measure ten fresh single-board camera startups on a Pi."""

from _camera_startup import main


if __name__ == "__main__":
    raise SystemExit(main("single_board_http.py", 1))
