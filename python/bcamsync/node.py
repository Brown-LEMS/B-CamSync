"""Serve node-local HTTP control with explicit or automatic network startup."""

from __future__ import annotations

import argparse
from pathlib import Path
import signal
import sys
import threading
from typing import TYPE_CHECKING

from . import NetworkSession
from .http_management import HttpNodeService
from .management import NodeService

if TYPE_CHECKING:
    from bcamsync_managed import Config


def run_node(config: Config, local_node: str, *, network_startup: str = "explicit",
             address: str | None = None, http_port: int = 58083, discovery_port: int = 58082,
             server_class=HttpNodeService, **server_options) -> None:
    """Serve on the main thread; capture starts only after an explicit control request.

    Automatic startup owns a separate network across capture restarts. Explicit
    startup uses an existing HTTP address and leaves network setup to the caller
    or to the synchronization session when capture is explicitly started.
    """
    if network_startup not in {"automatic", "explicit"}:
        raise ValueError("network_startup must be automatic or explicit")
    network = None
    if network_startup == "automatic":
        sync = config.sync_config()
        local = sync.node(local_node)
        if len(sync.nodes) == 1 and sync.network_mode == "auto" and not local.interface:
            if address is None:
                raise ValueError("single-node HTTP usage needs --address or a configured interface")
            nodes = sync.nodes
            nodes[0].address = address
            sync.nodes = nodes
            sync.network_mode = "existing"
        network = NetworkSession(sync, local_node)
    elif not (address or config.node(local_node).address):
        raise ValueError("explicit network startup requires --address or a configured local address")
    controller = NodeService(config, local_node, network=network)
    stopping = threading.Event()
    server = None
    previous = {}

    def terminate(*_):
        stopping.set()
        if server is not None:
            server.request_shutdown()
        controller.request_shutdown()
        if network is not None:
            network.request_stop()

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, terminate)
        if network is not None:
            try:
                network.start(config.startup_timeout_seconds)
            except RuntimeError:
                if stopping.is_set():
                    return
                raise
        if stopping.is_set():
            return
        bind_address = address or (network.address if network is not None else config.node(local_node).address)
        server = server_class(controller, bind_address, http_port=http_port,
                              discovery_port=discovery_port, **server_options)
        server.serve()
    finally:
        try:
            try:
                if server is not None:
                    server.close()
            finally:
                controller.close()
        finally:
            try:
                if network is not None:
                    network.stop()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("node")
    parser.add_argument("--network-startup", choices=("explicit", "automatic"), default="explicit",
                        help="automatic networking starts before HTTP and stays alive across capture restarts")
    parser.add_argument("--http-port", type=int, default=58083)
    parser.add_argument("--discovery-port", type=int, default=58082)
    parser.add_argument("--address", help="local HTTP IPv4 address; required for explicit startup without a configured address")
    args = parser.parse_args(argv)
    from bcamsync_managed import Config

    try:
        run_node(Config.from_file(args.config), args.node, network_startup=args.network_startup,
                 address=args.address, http_port=args.http_port, discovery_port=args.discovery_port)
    except (ValueError, KeyError, RuntimeError, OSError) as error:
        print(f"B-CamSync node: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
