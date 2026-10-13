"""Optional host control of node-local sessions; pixels remain on each node.

The foreground node service reads predeployed configuration and starts cameras
only after an explicit request. Closing an SSH client does not close its session.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import signal
import socket
import socketserver
import sys
import threading
from uuid import UUID, uuid4

from . import Config, Session


LIMIT = 1024 * 1024
COMMANDS = {"configuration", "start", "status", "stop"}


def _operation_id(value: str | None) -> str | None:
    if value is not None:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("operation_id must be a canonical UUID")
    return value


def _status(session, local_node: str, active: bool, operation_id: str | None) -> dict:
    if session is None:
        return {"node_id": local_node, "active": False, "operation_id": None,
                "state": "stopped", "ready": False, "fault": "", "units": []}
    status = session.status()
    return {"node_id": local_node, "active": active, "operation_id": operation_id,
            "state": status.state, "ready": status.ready, "fault": status.fault,
            "units": [{"unit_id": unit.unit_id, "state": unit.state, "ready": unit.ready,
                       "fault": unit.fault, "phase_ms": unit.phase_ms, "p95_ms": unit.p95_ms,
                       "max_ms": unit.max_ms, "measurement_end_ns": unit.measurement_end_ns}
                      for unit in status.units]}


class NodeService:
    """Own a local Session independently of connecting management clients."""

    def __init__(self, config: Config, local_node: str, socket_path: str | Path):
        config.validate()
        config.node(local_node)
        self.config = config
        self.local_node = local_node
        self.socket_path = Path(socket_path)
        if not self.socket_path.is_absolute():
            raise ValueError("socket_path must be absolute")
        self._session = None
        self._active = False
        self._operation = None
        self._closing = False
        self._lock = threading.RLock()
        self._shutdown = threading.Event()

    @property
    def session(self) -> Session | None:
        """Local frame access; None before first start, a stopped Session after stop."""
        with self._lock:
            return self._session

    def request(self, command: str, operation_id: str | None = None) -> dict:
        """Dispatch one local command; configuration inspection never starts cameras."""
        if not isinstance(command, str) or command not in COMMANDS:
            raise ValueError("unsupported management command")
        operation_id = _operation_id(operation_id)
        if operation_id is not None and command not in {"start", "stop"}:
            raise ValueError("operation_id applies only to start and stop")
        with self._lock:
            if self._closing:
                raise RuntimeError("node service is closing")
            if command == "configuration":
                return {"node_id": self.local_node, "config": json.loads(self.config.to_json()),
                        "sync_contract": json.loads(self.config.sync_json()),
                        "reference_node": self.config.reference_node()}
            if command == "start":
                if self._active:
                    raise RuntimeError("node session is already active; stop it explicitly first")
                if self._session is None:
                    self._session = Session(self.config, self.local_node)
                self._session.start()
                self._operation = operation_id or str(uuid4())
                self._active = True
            elif command == "stop":
                if operation_id is not None and self._active and self._operation != operation_id:
                    raise RuntimeError("node session belongs to a different management operation")
                if self._session is not None:
                    self._session.stop()
                self._active = False
                self._operation = None
            return _status(self._session, self.local_node, self._active, self._operation)

    def close(self) -> None:
        """Close the session and end the foreground service."""
        self.request_shutdown()
        with self._lock:
            self._closing = True
            if self._session is not None:
                self._session.stop()
            self._active = False
            self._operation = None

    def request_shutdown(self) -> None:
        """Set the shutdown flag; signal handlers leave cleanup to serve()."""
        self._shutdown.set()

    def serve(self) -> None:
        """Serve one bounded JSON request per Unix connection until explicitly closed."""
        service = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(service.config.startup_timeout_seconds + 20)
                try:
                    raw = self.rfile.readline(LIMIT + 1)
                    if len(raw) > LIMIT or not raw.endswith(b"\n"):
                        raise ValueError("management request exceeds limit or lacks newline")
                    payload = json.loads(raw)
                    if not isinstance(payload, dict) or set(payload) - {"command", "operation_id"}:
                        raise ValueError("invalid management request fields")
                    result = service.request(payload["command"], payload.get("operation_id"))
                    response = {"ok": True, "result": result}
                except (ValueError, KeyError, RuntimeError, OSError) as error:
                    response = {"ok": False, "error": str(error)}
                encoded = json.dumps(response, allow_nan=False).encode() + b"\n"
                if len(encoded) > LIMIT:
                    encoded = b'{"ok":false,"error":"management response exceeds limit"}\n'
                self.wfile.write(encoded)

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        if self.socket_path.exists():
            raise FileExistsError(f"Management socket already exists: {self.socket_path}")
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with Server(str(self.socket_path), Handler) as server:
            self.socket_path.chmod(0o600)
            server.timeout = 0.2
            try:
                while not self._shutdown.is_set():
                    server.handle_request()
            finally:
                try:
                    with self._lock:
                        self._closing = True
                        try:
                            if self._session is not None:
                                self._session.stop()
                        finally:
                            self._active = False
                            self._operation = None
                finally:
                    self.socket_path.unlink()


def request_local(socket_path: str | Path, command: str, *, timeout_seconds: float,
                  operation_id: str | None = None) -> dict:
    """Connect to a running node service without owning its session lifetime."""
    if command not in COMMANDS:
        raise ValueError("unsupported management command")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    payload = {"command": command}
    if operation_id is not None:
        payload["operation_id"] = _operation_id(operation_id)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout_seconds)
        connection.connect(str(socket_path))
        connection.sendall(json.dumps(payload).encode() + b"\n")
        with connection.makefile("rb") as stream:
            raw = stream.readline(LIMIT + 1)
    if len(raw) > LIMIT or not raw.endswith(b"\n"):
        raise RuntimeError("management response exceeds limit or lacks newline")
    response = json.loads(raw)
    if not isinstance(response, dict):
        raise RuntimeError("invalid management response")
    if response.get("ok") is not True:
        raise RuntimeError(str(response.get("error", "invalid management response")))
    return response["result"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("config", type=Path)
    serve.add_argument("node")
    serve.add_argument("--socket", required=True, type=Path)
    request = sub.add_parser("request")
    request.add_argument("command", choices=sorted(COMMANDS))
    request.add_argument("--socket", required=True, type=Path)
    request.add_argument("--timeout", type=float, default=75)
    request.add_argument("--operation-id")
    args = parser.parse_args(argv)
    try:
        if args.mode == "serve":
            service = NodeService(Config.from_file(args.config), args.node, args.socket)
            previous = {}

            def terminate(_signal, _frame):
                service.request_shutdown()

            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, terminate)
            try:
                service.serve()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        else:
            result = request_local(args.socket, args.command, timeout_seconds=args.timeout,
                                   operation_id=args.operation_id)
            print(json.dumps(result, indent=2, allow_nan=False))
    except (ValueError, KeyError, RuntimeError, OSError) as error:
        print(f"B-CamSync management: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
