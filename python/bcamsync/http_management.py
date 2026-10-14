"""Optional HTTP control and discovery of node applications; no image transport."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import signal
import socket
import threading
import time

from . import Config, Session
from ._native import _NetworkLink
from .management import LIMIT, NodeService, _operation_id


DISCOVERY_ADDRESS = "239.255.58.81"
DISCOVERY_PORT = 58082
DISCOVERY_PROTOCOL = "bcamsync-management-v1"


class ManagementHandler(BaseHTTPRequestHandler):
    """HTTP handler which examples can extend with their application routes."""

    def setup(self):
        super().setup()
        self.connection.settimeout(self.server.service.config.startup_timeout_seconds + 20)

    def log_message(self, _format, *args):
        pass

    def _reply(self, code: int, payload: dict):
        encoded = json.dumps(payload, allow_nan=False).encode()
        if len(encoded) > LIMIT:
            code, encoded = 500, b'{"ok":false,"error":"management response exceeds limit"}'
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _dispatch(self, command: str, operation_id: str | None = None):
        try:
            result = self.server.service.request(command, operation_id)
        except (ValueError, KeyError) as error:
            self._reply(400, {"ok": False, "error": str(error)})
        except (RuntimeError, OSError) as error:
            self._reply(409, {"ok": False, "error": str(error)})
        else:
            self._reply(200, {"ok": True, "result": result})

    def do_GET(self):
        command = self.path.removeprefix("/")
        if command not in {"configuration", "status"}:
            self._reply(404, {"ok": False, "error": "unknown management route"})
            return
        self._dispatch(command)

    def do_POST(self):
        command = self.path.removeprefix("/")
        if command not in {"start", "stop"}:
            self._reply(404, {"ok": False, "error": "unknown management route"})
            return
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("management requests require Content-Length")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= LIMIT:
                raise ValueError("management request exceeds limit")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete management request")
            payload = json.loads(raw) if raw else {}
            if not isinstance(payload, dict) or set(payload) - {"operation_id"}:
                raise ValueError("invalid management request fields")
        except (ValueError, UnicodeDecodeError) as error:
            self._reply(400, {"ok": False, "error": str(error)})
            return
        self._dispatch(command, payload.get("operation_id"))


class _Discovery:
    """Advertise one live HTTP service and observe peers on its local link."""

    def __init__(self, service, port):
        self.service = service
        self.port = port
        self.peers = {}
        self.fault = None
        self.changed = threading.Condition()
        self.stop_event = threading.Event()
        self.channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.channel.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.channel.bind(("0.0.0.0", port))
            membership = socket.inet_aton(DISCOVERY_ADDRESS) + socket.inet_aton(service.address)
            self.channel.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            # Receive only multicast joined on this link, even if another group uses this port.
            self.channel.setsockopt(socket.IPPROTO_IP, 49, 0)  # Linux IP_MULTICAST_ALL
            self.channel.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                    socket.inet_aton(service.address))
            self.channel.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            self.channel.settimeout(0.2)
        except BaseException:
            self.channel.close()
            raise
        self.thread = threading.Thread(target=self._run, name="bcamsync-http-discovery")

    def start(self):
        self.thread.start()

    def _run(self):
        service = self.service
        announcement = json.dumps({"protocol": DISCOVERY_PROTOCOL, "type": "announcement",
                                   "group_id": service.config.group_id, "node_id": service.local_node,
                                   "http_port": service.http_port}).encode()
        next_announcement = 0
        try:
            while not self.stop_event.is_set():
                now = time.monotonic()
                if now >= next_announcement:
                    self.channel.sendto(announcement, (DISCOVERY_ADDRESS, self.port))
                    next_announcement = now + 0.5
                try:
                    raw, source = self.channel.recvfrom(65535)
                except socket.timeout:
                    continue
                try:
                    message = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if (not isinstance(message, dict) or message.get("protocol") != DISCOVERY_PROTOCOL
                        or message.get("group_id") != service.config.group_id):
                    continue
                if message.get("type") == "query":
                    self.channel.sendto(announcement, source)
                elif message.get("type") == "announcement":
                    node = message.get("node_id")
                    port = message.get("http_port")
                    if (not isinstance(node, str) or node not in {item.id for item in service.config.nodes}
                            or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535):
                        continue
                    with self.changed:
                        previous = self.peers.get(node)
                        endpoint = (source[0], port)
                        if previous and now - previous[1] < 3 and previous[0] != endpoint:
                            raise RuntimeError(f"Multiple management endpoints advertise node {node}")
                        self.peers[node] = (endpoint, time.monotonic())
                        self.changed.notify_all()
        except (OSError, RuntimeError) as error:
            with self.changed:
                self.fault = error
                self.changed.notify_all()

    def addresses(self):
        deadline = time.monotonic() + self.service.config.startup_timeout_seconds
        expected = {node.id for node in self.service.config.nodes}
        with self.changed:
            while True:
                if self.fault:
                    raise RuntimeError(f"Management discovery failed: {self.fault}")
                if self.service._shutdown.is_set():
                    raise RuntimeError("Management service is closing")
                now = time.monotonic()
                found = {node: endpoint[0] for node, (endpoint, seen) in self.peers.items() if now - seen < 3}
                found[self.service.local_node] = self.service.address
                if expected <= found.keys():
                    return found
                if now >= deadline:
                    raise RuntimeError("Timed out discovering configured management nodes")
                self.changed.wait(min(0.2, deadline - now))

    def close(self):
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join()
        self.channel.close()


class HttpNodeService(NodeService):
    """Optional application service; its network outlives acquisition start/stop.

    Auto mode activates the configured interface once for this service lifetime.
    Existing mode uses its configured numeric IPv4. An explicit address permits
    management of a single-node configuration without network configuration.
    """

    def __init__(self, config: Config, local_node: str, *, http_port: int = 58083,
                 discovery_port: int = DISCOVERY_PORT, address: str | None = None,
                 handler_class=ManagementHandler):
        super().__init__(config, local_node)
        for port in (http_port, discovery_port):
            if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
                raise ValueError("service ports must be between 0 and 65535")
        if discovery_port == 0:
            raise ValueError("discovery port must not be zero")
        self.http_port = http_port
        self.discovery_port = discovery_port
        self.address = address
        self.handler_class = handler_class
        self._link = None
        self._discovery = None
        self._server = None
        self.listening = threading.Event()

    def _create_session(self):
        if self._discovery is None:
            raise RuntimeError("HTTP service must be serving before acquisition starts")
        resolved = Config(self.config)
        if resolved.network_mode == "auto" and len(resolved.nodes) > 1:
            addresses = self._discovery.addresses()
            nodes = resolved.nodes
            for node in nodes:
                node.address = addresses[node.id]
            resolved.nodes = nodes
            resolved.network_mode = "existing"
        return Session(resolved, self.local_node)

    def request(self, command, operation_id=None):
        operation_id = _operation_id(operation_id)
        with self._lock:
            if self._closing or self._shutdown.is_set():
                raise RuntimeError("node service is closing")
            if command == "start" and not self._active and self._session is not None:
                if self.config.network_mode == "auto" and len(self.config.nodes) > 1:
                    self._session.set_node_addresses(self._discovery.addresses())
            return super().request(command, operation_id)

    def serve(self):
        """Establish the management link, then serve until request_shutdown()."""
        if self._server is not None or self._closing:
            raise RuntimeError("HTTP service is already serving or closed")
        try:
            if self.address is None:
                local = self.config.node(self.local_node)
                if self.config.network_mode == "auto":
                    self._link = _NetworkLink(local.interface, f"bcamsync-http-{self.config.group_id}-{self.local_node}")
                    print(f"Activating management link on {local.interface}", flush=True)
                    self.address = self._link.start(self.config.startup_timeout_seconds)
                else:
                    self.address = local.address
            address = ipaddress.IPv4Address(self.address)
            if address.is_unspecified or address.is_multicast:
                raise ValueError("management requires a local unicast IPv4 address")
            with ThreadingHTTPServer((self.address, self.http_port), self.handler_class) as server:
                self._server = server
                server.service = self
                server.timeout = 0.2
                self.http_port = server.server_address[1]
                self._discovery = _Discovery(self, self.discovery_port)
                self._discovery.start()
                self.listening.set()
                print(f"B-CamSync management: http://{self.address}:{self.http_port}", flush=True)
                while not self._shutdown.is_set():
                    if self._discovery.fault:
                        raise RuntimeError(f"Management discovery failed: {self._discovery.fault}")
                    server.handle_request()
        finally:
            self.listening.clear()
            self.request_shutdown()
            try:
                super().close()
            finally:
                try:
                    if self._discovery is not None:
                        self._discovery.close()
                finally:
                    if self._link is not None:
                        self._link.stop()
                    self._server = None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("node")
    parser.add_argument("--http-port", type=int, default=58083)
    parser.add_argument("--discovery-port", type=int, default=DISCOVERY_PORT)
    parser.add_argument("--address", help="existing local IPv4 address; do not activate a connection")
    args = parser.parse_args(argv)
    service = HttpNodeService(Config.from_file(args.config), args.node, http_port=args.http_port,
                              discovery_port=args.discovery_port, address=args.address)
    previous = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: service.request_shutdown())
        service.serve()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    main()
