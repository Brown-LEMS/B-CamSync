"""Optional HTTP and SSH management using only the Python standard library."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Mapping
from uuid import uuid4


LIMIT = 1024 * 1024


def _sync_contract(config: Mapping) -> dict:
    """Normalize only group membership, reference, clock domain, and frame rate."""
    nodes = sorted(node["id"] for node in config["nodes"])
    units = sorted(({"id": unit["id"], "node_id": unit["node_id"]}
                    for unit in config["units"]), key=lambda unit: unit["id"])
    if not nodes or len(set(nodes)) != len(nodes) or not units:
        raise ValueError("configuration must contain unique nodes and nonempty units")
    if len({unit["id"] for unit in units}) != len(units):
        raise ValueError("unit IDs must be unique")
    if any(unit["node_id"] not in nodes for unit in units):
        raise ValueError("unit belongs to an unknown node")
    if config["reference_unit"] not in {unit["id"] for unit in units}:
        raise ValueError("reference_unit must identify a configured unit")
    clock_mode = config.get("clock_mode", "host" if config.get("backend") == "fake" else "network")
    if clock_mode not in ("network", "host"):
        raise ValueError("clock_mode must be network or host")
    return {"group_id": config["group_id"], "reference_unit": config["reference_unit"],
            "clock_mode": clock_mode, "fps": config.get("fps", 40),
            "clock_domain": "local" if len(nodes) == 1 else "common",
            "nodes": nodes, "units": units}


@dataclass(frozen=True)
class NodeTarget:
    """SSH access to one existing node service; Python selects its installed environment."""

    ssh_host: str
    python: str
    socket: str

    def __post_init__(self):
        if not isinstance(self.ssh_host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:\[\]-]*", self.ssh_host):
            raise ValueError("ssh_host must be an SSH destination or configured alias")
        if not isinstance(self.python, str) or not self.python or not Path(self.socket).is_absolute():
            raise ValueError("python must name the remote interpreter and socket must be absolute")


class GroupClient:
    """Manage nodes using a JSON configuration; local capture settings may differ."""

    def __init__(self, config: Mapping, targets: Mapping[str, NodeTarget], *, timeout_seconds: float | None = None):
        self.sync_contract = _sync_contract(config)
        if set(targets) != set(self.sync_contract["nodes"]):
            raise ValueError("targets must identify every configured node exactly once")
        self.reference_node = next(unit["node_id"] for unit in self.sync_contract["units"]
                                   if unit["id"] == self.sync_contract["reference_unit"])
        self.targets = dict(targets)
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else config.get("startup_timeout_seconds", 60) + 20
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")

    def _request(self, node_id: str, command: str, operation_id: str | None = None) -> dict:
        target = self.targets[node_id]
        remote = [target.python, "-m", "bcamsync.management", "request", command,
                  "--socket", target.socket, "--timeout", str(self.timeout_seconds)]
        if operation_id is not None:
            remote += ["--operation-id", operation_id]
        completed = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                                    target.ssh_host, shlex.join(remote)],
                                   check=False, capture_output=True, text=True,
                                   timeout=self.timeout_seconds + 10)
        if completed.returncode:
            raise RuntimeError(f"Node {node_id} {command} failed: {completed.stderr.strip()}")
        if len(completed.stdout.encode()) > LIMIT:
            raise RuntimeError("remote management response exceeds limit")
        response = json.loads(completed.stdout)
        if not isinstance(response, dict) or response.get("node_id") != node_id:
            raise RuntimeError("remote node identity does not match its target")
        return response

    def check(self) -> dict:
        """Inspect shared synchronization settings without imposing local capture settings."""
        expected = self.sync_contract
        reports = {}
        for node_id in self.sync_contract["nodes"]:
            print(f"Checking synchronization contract on {node_id}", file=sys.stderr, flush=True)
            report = self._request(node_id, "configuration")
            if report.get("sync_contract") != expected or report.get("reference_node") != self.reference_node:
                raise RuntimeError(f"Synchronization contract differs on node {node_id}")
            reports[node_id] = report
        return {"sync_contract_matches": True, "nodes": reports}

    def status(self) -> dict:
        nodes = {}
        for node_id in self.sync_contract["nodes"]:
            print(f"Reading node status on {node_id}", file=sys.stderr, flush=True)
            nodes[node_id] = self._request(node_id, "status")
        return {"reference_node": self.reference_node,
                "group": nodes[self.reference_node], "nodes": nodes}

    def start(self) -> dict:
        """Check all nodes, then start followers before the reference node."""
        self.check()
        print("Checking that all node sessions are inactive", file=sys.stderr, flush=True)
        states = self.status()["nodes"]
        if any(state["active"] for state in states.values()):
            raise RuntimeError("A node session is already active; stop it explicitly first")
        reference = self.reference_node
        order = [node_id for node_id in self.sync_contract["nodes"] if node_id != reference] + [reference]
        operation = str(uuid4())
        attempted = []
        started = {}
        try:
            for node_id in order:
                print(f"Starting node {node_id}", file=sys.stderr, flush=True)
                attempted.append(node_id)
                started[node_id] = self._request(node_id, "start", operation)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            cleanup_errors = {}
            for node_id in reversed(attempted):
                print(f"Cleaning up operation {operation} on {node_id}", file=sys.stderr, flush=True)
                try:
                    self._request(node_id, "stop", operation)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as cleanup:
                    cleanup_errors[node_id] = str(cleanup)
            raise RuntimeError(f"Group start failed: {error}; operation cleanup errors: {cleanup_errors}") from error
        return {"operation_id": operation, "reference_node": reference, "nodes": started}

    def stop(self) -> dict:
        """Stop coordination first and then clean up every node-local session."""
        reference = self.reference_node
        order = [reference] + [node_id for node_id in self.sync_contract["nodes"] if node_id != reference]
        nodes = {}
        errors = {}
        for node_id in order:
            print(f"Stopping node {node_id}", file=sys.stderr, flush=True)
            try:
                nodes[node_id] = self._request(node_id, "stop")
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                errors[node_id] = str(error)
        if errors:
            raise RuntimeError(f"Group stop failed on nodes: {errors}")
        return {"reference_node": reference, "nodes": nodes}


from .http import HttpGroupClient, HttpNodeTarget, discover, interface_addresses
