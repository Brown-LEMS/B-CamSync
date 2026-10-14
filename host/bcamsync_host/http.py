"""HTTP control and IPv4 multicast discovery for optional node services."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import math
import re
import socket
import subprocess
import sys
import time
from typing import Mapping
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from . import GroupClient, LIMIT, _sync_contract


DISCOVERY_ADDRESS = "239.255.58.81"
DISCOVERY_PORT = 58082
DISCOVERY_PROTOCOL = "bcamsync-management-v1"


@dataclass(frozen=True)
class HttpNodeTarget:
    """HTTP endpoint of one node service, supplied explicitly or discovered."""

    base_url: str

    def __post_init__(self):
        parsed = urlsplit(self.base_url)
        if (parsed.scheme != "http" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment):
            raise ValueError("base_url must be an HTTP origin, without credentials or a path")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("HTTP port must be between 1 and 65535")


class HttpGroupClient(GroupClient):
    """Use the same group orchestration as GroupClient through HTTP endpoints."""

    def __init__(self, config: Mapping, targets: Mapping[str, HttpNodeTarget], *, timeout_seconds: float | None = None):
        expected = set(_sync_contract(config)["nodes"])
        missing, unexpected = sorted(expected - set(targets)), sorted(set(targets) - expected)
        if missing or unexpected:
            raise ValueError(f"HTTP node membership differs: missing={missing}, unexpected={unexpected}")
        super().__init__(config, targets, timeout_seconds=timeout_seconds)

    def _request(self, node_id: str, command: str, operation_id: str | None = None) -> dict:
        if command not in ("configuration", "status", "start", "stop"):
            raise ValueError("unknown management command")
        body = None
        if command in ("start", "stop"):
            body = json.dumps({"operation_id": operation_id} if operation_id is not None else {}).encode()
        request = Request(self.targets[node_id].base_url.rstrip("/") + "/" + command,
                          data=body, headers={"Content-Type": "application/json"})
        opener = build_opener(ProxyHandler({}))
        try:
            response = opener.open(request, timeout=self.timeout_seconds)
        except HTTPError as error:
            response = error
        with response:
            payload = response.read(LIMIT + 1)
            if len(payload) > LIMIT:
                raise RuntimeError("remote management response exceeds limit")
            envelope = json.loads(payload)
            if not isinstance(envelope, dict) or envelope.get("ok") is not True:
                detail = envelope.get("error", "invalid response") if isinstance(envelope, dict) else "invalid response"
                raise RuntimeError(f"Node {node_id} {command} failed: {detail}")
            if response.status != 200:
                raise RuntimeError(f"Node {node_id} {command} failed: HTTP {response.status}")
            result = envelope.get("result")
            if not isinstance(result, dict) or result.get("node_id") != node_id:
                raise RuntimeError("remote node identity does not match its target")
            return result


def interface_addresses(interface: str | None = None) -> list[str]:
    """Read active local IPv4 interfaces without changing their configuration."""
    addresses = []
    if sys.platform == "darwin":
        command = ["/sbin/ifconfig"] + ([interface] if interface else [])
        output = subprocess.run(command, check=True, capture_output=True, text=True).stdout
        active = False
        for line in output.splitlines():
            if line and not line[0].isspace():
                active = "<UP," in line or ",UP," in line
            match = re.match(r"\s+inet (\S+)\s", line)
            if active and match and not ipaddress.IPv4Address(match[1]).is_loopback:
                addresses.append(match[1])
    elif sys.platform.startswith("linux"):
        command = ["ip", "-j", "-4", "addr", "show", "up"] + (["dev", interface] if interface else [])
        output = subprocess.run(command, check=True, capture_output=True, text=True).stdout
        for device in json.loads(output):
            for entry in device.get("addr_info", []):
                if entry.get("family") == "inet" and not ipaddress.IPv4Address(entry["local"]).is_loopback:
                    addresses.append(entry["local"])
    else:
        raise RuntimeError("automatic interface selection supports macOS and Linux; supply an IPv4 address")
    return list(dict.fromkeys(addresses))


def discover(group_id: str, timeout_seconds: float = 2, address: str = "0.0.0.0",
             port: int = DISCOVERY_PORT, *, interface: str | None = None) -> dict[str, HttpNodeTarget]:
    """Query node services on local interfaces; use each reply's source IP as its endpoint."""
    if not isinstance(group_id, str) or not group_id:
        raise ValueError("group_id must be nonempty")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("discovery port must be between 1 and 65535")
    ipaddress.IPv4Address(address)
    if interface is not None and address != "0.0.0.0":
        raise ValueError("select either interface or address")
    addresses = interface_addresses(interface) if address == "0.0.0.0" else [address]
    if not addresses:
        raise RuntimeError("no active IPv4 interface is available for discovery")
    query = json.dumps({"protocol": DISCOVERY_PROTOCOL, "type": "query", "group_id": group_id}).encode()
    targets = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
        channel.bind(("0.0.0.0", 0))
        channel.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        deadline = time.monotonic() + timeout_seconds
        next_query = 0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_query:
                for local in addresses:
                    channel.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local))
                    channel.sendto(query, (DISCOVERY_ADDRESS, port))
                next_query = now + 0.5
            channel.settimeout(max(0.001, min(deadline, next_query) - time.monotonic()))
            try:
                data, source = channel.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                announcement = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                continue
            if (not isinstance(announcement, dict) or announcement.get("protocol") != DISCOVERY_PROTOCOL
                    or announcement.get("type") != "announcement" or announcement.get("group_id") != group_id):
                continue
            node_id = announcement.get("node_id")
            http_port = announcement.get("http_port")
            if (not isinstance(node_id, str) or not node_id or isinstance(http_port, bool)
                    or not isinstance(http_port, int) or not 1 <= http_port <= 65535):
                continue
            target = HttpNodeTarget(f"http://{source[0]}:{http_port}")
            if node_id in targets and targets[node_id] != target:
                raise RuntimeError(f"Multiple management endpoints advertise node {node_id}")
            targets[node_id] = target
    return targets
