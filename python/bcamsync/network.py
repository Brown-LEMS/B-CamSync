"""Run optional node networking and discovery without initializing cameras."""

import argparse
from pathlib import Path
import signal
import sys
import threading

from . import NetworkSession, SyncConfig


def run_network(config: SyncConfig, local_node: str) -> None:
    """Start local networking, announce until SIGINT/SIGTERM, then release owned resources.

    Run on the main thread. Missing peers do not end the service or start capture.
    """
    network = NetworkSession(config, local_node)
    stopping = threading.Event()
    previous = {}

    def terminate(*_):
        stopping.set()
        network.request_stop()

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, terminate)
        try:
            network.start(config.startup_timeout_seconds)
        except RuntimeError:
            if stopping.is_set():
                return
            raise
        print(f"B-CamSync network: {network.address} ({local_node})", flush=True)
        while not stopping.wait(0.2):
            if network.fault:
                raise RuntimeError(network.fault)
    finally:
        try:
            network.stop()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("node")
    args = parser.parse_args(argv)
    try:
        run_network(SyncConfig.from_file(args.config), args.node)
    except (ValueError, KeyError, RuntimeError, OSError) as error:
        print(f"B-CamSync network: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
