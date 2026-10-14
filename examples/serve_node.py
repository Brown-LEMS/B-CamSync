"""Compose managed capture, session networking, and optional HTTP control.

The library HTTP transport accepts an existing controller. This application
chooses camera ownership and keeps its network alive across acquisition restarts.
"""

import argparse
from pathlib import Path
import signal

from bcamsync import Config, NetworkSession, Session
from bcamsync.http_management import HttpNodeService
from bcamsync.management import NodeService, _operation_id


class ManagedNode(NodeService):
    """Example ownership of managed capture on a separately owned network session."""

    def __init__(self, config, local_node, network):
        super().__init__(config, local_node)
        self.network = network

    def _create_session(self):
        resolved = self.network.resolve(self.config.startup_timeout_seconds)
        config = Config(self.config)
        config.nodes = resolved.nodes
        config.network_mode = "existing"
        return Session(config, self.local_node)

    def request(self, command, operation_id=None):
        operation_id = _operation_id(operation_id)
        with self._lock:
            if self._closing or self._shutdown.is_set():
                raise RuntimeError("node service is closing")
            if command == "start" and not self._active and self._session is not None:
                resolved = self.network.resolve(self.config.startup_timeout_seconds)
                self._session.set_node_addresses({node.id: node.address for node in resolved.nodes})
            return super().request(command, operation_id)


def run_node(config, local_node, *, address=None, http_port=58083,
             discovery_port=58082, server_class=HttpNodeService, **server_options):
    """Compose resources here rather than hiding application ownership in HTTP."""
    sync = config.sync_config()
    # Single-node capture has no synchronization link; select the existing
    # address explicitly when the example needs remote management instead.
    local = sync.node(local_node)
    if len(sync.nodes) == 1 and sync.network_mode == "auto" and not local.interface:
        if address is None:
            raise ValueError("single-node HTTP usage needs --address or a configured interface")
        nodes = sync.nodes
        nodes[0].address = address
        sync.nodes = nodes
        sync.network_mode = "existing"
    network = NetworkSession(sync, local_node)
    controller = ManagedNode(config, local_node, network)
    server = None
    previous = {}
    try:
        network.start(config.startup_timeout_seconds)
        # The management address may differ from the synchronization link.
        server = server_class(controller, address or network.address, http_port=http_port,
                              discovery_port=discovery_port, **server_options)

        def terminate(*_):
            server.request_shutdown()
            controller.request_shutdown()
            network.request_stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, terminate)
        server.serve()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        try:
            if server is not None:
                server.close()
            controller.close()
        finally:
            network.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("node")
    parser.add_argument("--http-port", type=int, default=58083)
    parser.add_argument("--discovery-port", type=int, default=58082)
    parser.add_argument("--address", help="existing local HTTP address; synchronization uses its configured link")
    args = parser.parse_args()
    run_node(Config.from_file(args.config), args.node, address=args.address,
             http_port=args.http_port, discovery_port=args.discovery_port)


if __name__ == "__main__":
    main()
