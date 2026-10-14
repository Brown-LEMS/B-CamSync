"""Optional HTTP control and discovery of node applications; no image transport."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import socket
import threading
import time

from .management import LIMIT, NodeService


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
    """Advertise only this HTTP endpoint; synchronization discovery is separate."""

    def __init__(self, service, port):
        self.service, self.port = service, port
        self.fault = None
        self.stop_event = threading.Event()
        self.channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.channel.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.channel.bind(("0.0.0.0", port))
            membership = socket.inet_aton(DISCOVERY_ADDRESS) + socket.inet_aton(service.address)
            self.channel.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            self.channel.setsockopt(socket.IPPROTO_IP, 49, 0)  # Linux IP_MULTICAST_ALL
            self.channel.settimeout(0.2)
            # Replies must originate from the address where HTTP is actually listening.
            self.sender.bind((service.address, 0))
            self.sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(service.address))
            self.sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        except BaseException:
            self.channel.close()
            self.sender.close()
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
                    self.sender.sendto(announcement, (DISCOVERY_ADDRESS, self.port))
                    next_announcement = now + 0.5
                try:
                    raw, source = self.channel.recvfrom(65535)
                except socket.timeout:
                    continue
                try:
                    message = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if (isinstance(message, dict) and message.get("protocol") == DISCOVERY_PROTOCOL
                        and message.get("group_id") == service.config.group_id and message.get("type") == "query"):
                    self.sender.sendto(announcement, source)
        except OSError as error:
            self.fault = error

    def close(self):
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join()
        self.channel.close()
        self.sender.close()


class HttpNodeService:
    """HTTP transport for a caller-supplied controller and already available address.

    It creates no capture session or network connection. Closing this transport
    leaves the controller and its managed or external session with their caller.
    """

    def __init__(self, controller: NodeService, address: str, *, http_port: int = 58083,
                 discovery_port: int = DISCOVERY_PORT, handler_class=ManagementHandler):
        address_value = ipaddress.IPv4Address(address)
        if address_value.is_unspecified or address_value.is_multicast:
            raise ValueError("management requires a local unicast IPv4 address")
        for port in (http_port, discovery_port):
            if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
                raise ValueError("service ports must be between 0 and 65535")
        if discovery_port == 0:
            raise ValueError("discovery port must not be zero")
        self.controller = controller
        self.config = controller.config
        self.local_node = controller.local_node
        self.address = address
        self.http_port = http_port
        self.discovery_port = discovery_port
        self.handler_class = handler_class
        self._discovery = None
        self._server = None
        self._shutdown = threading.Event()
        self._closed = False
        self.listening = threading.Event()

    @property
    def session(self):
        return self.controller.session

    def request(self, command, operation_id=None):
        return self.controller.request(command, operation_id)

    def request_shutdown(self):
        self._shutdown.set()

    def close(self):
        """Request transport shutdown without changing the controlled session."""
        self.request_shutdown()

    def serve(self):
        if self._server is not None or self._closed:
            raise RuntimeError("HTTP service is already serving or closed")
        try:
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
                if self._discovery is not None:
                    self._discovery.close()
            finally:
                self._server = None
                self._closed = True
