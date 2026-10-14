"""Start the packaged node service with independent automatic networking."""

import sys

from bcamsync.node import main


if __name__ == "__main__":
    raise SystemExit(main(["--network-startup", "automatic", *sys.argv[1:]]))
