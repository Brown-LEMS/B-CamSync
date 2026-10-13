"""Operator-run Raspberry Pi OS network and chrony provisioning.

Planning is portable and read-only. Preparation and restoration require root on
Linux; acquisition applications call the installed, argument-free clock helper
before opening cameras. No device or SSH access is performed by this module.
"""

from __future__ import annotations

import argparse
import calendar
from datetime import datetime
import ipaddress
import json
import math
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time


CHRONY_CONFIG = Path("/etc/chrony/chrony.conf")
STATE_ROOT = Path("/var/lib/bcamsync")
HELPER_ROOT = Path("/usr/local/libexec")
SUDOERS_ROOT = Path("/etc/sudoers.d")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be an alphanumeric identifier (with _ -)")
    return value


def make_plan(config: dict, node_id: str, app_user: str | None = None) -> dict:
    """Return exact local configuration and commands without changing the host."""
    group = _identifier(config["group_id"], "group_id")
    backend = config.get("backend", "arducam")
    if backend not in ("arducam", "fake"):
        raise ValueError("backend must be arducam or fake")
    fake = backend == "fake"
    _identifier(node_id, "node_id")
    nodes = config["nodes"]
    if not nodes or len({n["id"] for n in nodes}) != len(nodes):
        raise ValueError("nodes must have unique identities")
    for entry in nodes:
        _identifier(entry["id"], "node_id")
        if not fake:
            _identifier(entry["interface"], "interface")
        ipaddress.IPv4Address(entry["address"])
        port = entry.get("port", 58080)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("node port must be an integer in 1..65535")
    endpoints = {(n["address"], n.get("port", 58080)) for n in nodes}
    if len(endpoints) != len(nodes):
        raise ValueError("nodes must have distinct IPv4 endpoints")
    if not fake and len({n["address"] for n in nodes}) != len(nodes):
        raise ValueError("physical nodes must have distinct IPv4 addresses")
    local = next(n for n in nodes if n["id"] == node_id)
    reference_unit = next(u for u in config["units"] if u["id"] == config["reference_unit"])
    reference = next(n for n in nodes if n["id"] == reference_unit["node_id"])
    network = config.get("network", {"managed": False})
    managed = network["managed"]
    if not isinstance(managed, bool):
        raise ValueError("network.managed must be a boolean")
    managed = managed and len(nodes) > 1 and not fake
    subnet = ipaddress.IPv4Network(network["subnet"]) if managed else None
    if subnet is not None:
        private_subnets = (ipaddress.IPv4Network("10.0.0.0/8"),
                           ipaddress.IPv4Network("172.16.0.0/12"),
                           ipaddress.IPv4Network("192.168.0.0/16"))
        if not any(subnet.subnet_of(private) for private in private_subnets):
            raise ValueError("managed synchronization subnet must be private")
        for entry in nodes:
            address = ipaddress.IPv4Address(entry["address"])
            if address not in subnet or address in (subnet.network_address, subnet.broadcast_address):
                raise ValueError(f"node {entry['id']} does not have a usable subnet address")
    offset = float(config.get("clock_max_offset_ms", 0.1)) / 1000
    max_age = float(config.get("clock_max_age_seconds", 10))
    timeout = float(config.get("startup_timeout_seconds", 60))
    if any(not math.isfinite(x) or x <= 0 for x in (offset, timeout, max_age)):
        raise ValueError("clock offset and startup timeout must be positive finite numbers")
    profile = f"bcamsync-{group}-{node_id}"
    commands = []
    if managed:
        commands = [[
            "nmcli", "connection", "add", "type", "ethernet", "ifname", local["interface"],
            "con-name", profile, "ipv4.method", "manual", "ipv4.addresses",
            f"{local['address']}/{subnet.prefixlen}", "ipv4.never-default", "yes",
            "ipv4.ignore-auto-dns", "yes", "ipv6.method", "disabled",
            "connection.autoconnect", "yes", "connection.autoconnect-priority", "100",
        ], ["nmcli", "connection", "up", "id", profile]]
    clock = len(nodes) > 1 and not fake
    lines = ["# Managed by B-CamSync. Restore with the node setup tool.",
             "driftfile /var/lib/chrony/chrony.drift", "rtcsync", "maxupdateskew 100.0"]
    if clock and local["id"] == reference["id"]:
        lines += ["local stratum 8", f"bindaddress {local['address']}"]
        lines += [f"allow {n['address']}/32" for n in nodes if n["id"] != node_id]
    elif clock:
        lines += [f"server {reference['address']} iburst minpoll 0 maxpoll 0", "port 0"]
    helper_name = f"bcamsync-clock-{group}-{node_id}"
    helper = HELPER_ROOT / helper_name
    wait_seconds = max(1, math.ceil(timeout) - 5)
    helper_lines = ["#!/bin/sh", "set -eu", '[ "$#" -eq 0 ]',
                    'export PATH=/usr/sbin:/usr/bin:/sbin:/bin',
                    'export LC_ALL=C',
                    'echo "B-CamSync: preparing clock before capture"']
    if local["id"] != reference["id"]:
        helper_lines += ["trap '/usr/bin/timeout -k 1s 1s /usr/bin/chronyc makestep 0 0 >/dev/null' EXIT",
                         f"/usr/bin/timeout -k 1s 1s /usr/bin/chronyc makestep {offset:.9f} 1",
                         "/usr/bin/timeout -k 1s 1s /usr/bin/chronyc burst 4/4"]
    helper_lines += [f"/usr/bin/timeout -k 1s {wait_seconds}s /usr/bin/chronyc -n waitsync {wait_seconds} {offset:.9f} 0 1",
                     "/usr/bin/timeout -k 1s 1s /usr/bin/chronyc -n tracking"]
    if app_user is not None:
        _identifier(app_user, "app_user")
    return {
        "group_id": group, "backend": backend, "node_id": node_id, "reference_node": reference["id"],
        "node_address": local["address"],
        "reference_address": reference["address"], "clock_max_offset_seconds": offset,
        "clock_max_age_seconds": max_age,
        "managed_network": managed, "interface": local.get("interface", ""), "profile": profile,
        "network_commands": commands, "clock_managed": clock,
        "chrony_config": "\n".join(lines) + "\n" if clock else None,
        "clock_helper": str(helper) if clock else None,
        "clock_helper_script": "\n".join(helper_lines) + "\n" if clock else None,
        "sudoers": f'{app_user} ALL=(root) NOPASSWD: {helper} ""\n' if app_user and clock else None,
        "app_user": app_user,
        "clock_prepare_command": ["sudo", "-n", str(helper)] if clock else [],
    }


def _run(command: list[str], *, capture: bool = False, timeout_seconds: float = 90) -> str:
    print("+ " + shlex.join(command), flush=True)
    result = subprocess.run(command, check=True, text=True, capture_output=capture, timeout=timeout_seconds,
                            env=dict(os.environ, LC_ALL="C"))
    return result.stdout.strip() if capture else ""


def _root_linux() -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("prepare/restore must be run as root on the Linux camera node")


def _state_path(plan: dict) -> Path:
    return STATE_ROOT / plan["group_id"] / plan["node_id"]


def _root_directory(path: Path, *, create: bool = False) -> None:
    """Require root-controlled directory ancestry for privileged files."""
    for directory in reversed((path.resolve(), *path.resolve().parents)):
        if not directory.exists() and create:
            directory.mkdir(mode=0o755)
        info = directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError(f"Privileged directory must be root-owned and not group/world writable: {directory}")


def prepare(plan: dict) -> None:
    """Install one node's profile and clock helper, retaining restore metadata."""
    if not plan["managed_network"] and not plan["clock_managed"]:
        print("Fake backend uses local software endpoints; no provisioning required" if plan["backend"] == "fake"
              else "Single-node group uses the local clock; no node provisioning required")
        return
    _root_linux()
    required = ["nmcli"] if plan["managed_network"] else []
    if plan["clock_managed"]:
        required += ["chronyc", "systemctl", "visudo", "timeout"]
        if not plan["app_user"]:
            raise ValueError("prepare requires --app-user for the clock helper")
        pwd.getpwnam(plan["app_user"])
    for tool in required:
        if shutil.which(tool) is None:
            raise RuntimeError(f"Required dependency {tool} is missing; install it before preparation")
    state_dir = _state_path(plan)
    if state_dir.exists():
        raise RuntimeError(f"Preparation already exists at {state_dir}; restore before changing topology")
    if plan["clock_managed"]:
        active_deployments = list(STATE_ROOT.glob("*/*/state.json"))
        if active_deployments:
            raise RuntimeError(f"Restore the active node deployment first: {active_deployments[0].parent}")
    previous = ""
    if plan["managed_network"]:
        profiles = _run(["nmcli", "-t", "-f", "NAME", "connection", "show"], capture=True).splitlines()
        if plan["profile"] in profiles:
            raise RuntimeError(f"Network profile {plan['profile']} already exists")
        previous = _run(["nmcli", "-g", "GENERAL.CON-UUID", "device", "show", plan["interface"]], capture=True)
        if previous == "--":
            previous = ""
    if plan["clock_managed"] and not CHRONY_CONFIG.is_file():
        raise RuntimeError("Install chrony before preparing this node")
    helper = Path(plan["clock_helper"]) if plan["clock_managed"] else None
    sudoers = SUDOERS_ROOT / plan["profile"]
    if helper is not None and (helper.exists() or sudoers.exists()):
        raise RuntimeError("Clock helper or sudoers file already exists; restore its owning deployment first")
    _root_directory(STATE_ROOT, create=True)
    if helper is not None:
        _root_directory(HELPER_ROOT, create=True)
        _root_directory(SUDOERS_ROOT)
    state_dir.mkdir(parents=True, mode=0o700)
    state = dict(plan, previous_profile=previous)
    (state_dir / "state.json").write_text(json.dumps(state, indent=2) + "\n")
    if plan["clock_managed"]:
        shutil.copy2(CHRONY_CONFIG, state_dir / "chrony.conf.original")
    print("[1/4] Saving node configuration for restoration", flush=True)
    for command in plan["network_commands"]:
        _run(command)
    print("[2/4] Synchronization network prepared", flush=True)
    if plan["clock_managed"]:
        CHRONY_CONFIG.write_text(plan["chrony_config"])
        helper.write_text(plan["clock_helper_script"])
        helper.chmod(0o755)
        staged_sudoers = state_dir / "sudoers"
        staged_sudoers.write_text(plan["sudoers"])
        staged_sudoers.chmod(0o440)
        _run(["visudo", "-cf", str(staged_sudoers)])
        shutil.copy2(staged_sudoers, sudoers)
        _run(["systemctl", "restart", "chrony.service"])
    print("[3/4] Clock configuration and restricted startup helper installed", flush=True)
    print("[4/4] Node prepared; prepare all peers before starting acquisition", flush=True)


def restore(plan: dict) -> None:
    """Restore the original chrony file and NetworkManager connection."""
    if not plan["managed_network"] and not plan["clock_managed"]:
        print("Fake backend has no physical network or clock to restore" if plan["backend"] == "fake"
              else "Single-node group has no provisioned network or clock to restore")
        return
    _root_linux()
    state_dir = _state_path(plan)
    state = json.loads((state_dir / "state.json").read_text())
    print("[1/3] Restoring chrony configuration", flush=True)
    if state["clock_managed"]:
        shutil.copy2(state_dir / "chrony.conf.original", CHRONY_CONFIG)
        _run(["systemctl", "restart", "chrony.service"])
        Path(state["clock_helper"]).unlink(missing_ok=True)
        (SUDOERS_ROOT / state["profile"]).unlink(missing_ok=True)
    print("[2/3] Restoring the previous network profile", flush=True)
    if state["managed_network"]:
        if state["previous_profile"]:
            _run(["nmcli", "connection", "up", "uuid", state["previous_profile"]])
        profiles = _run(["nmcli", "-t", "-f", "NAME", "connection", "show"], capture=True).splitlines()
        if state["profile"] in profiles:
            _run(["nmcli", "connection", "delete", "id", state["profile"]])
    shutil.rmtree(state_dir)
    print("[3/3] Original node configuration restored", flush=True)


def validate_tracking(plan: dict, output: str, *, now: float | None = None) -> None:
    """Require the configured group clock source, bounded offset, and freshness."""
    fields = dict(line.split(":", 1) for line in output.splitlines() if ":" in line)
    fields = {key.strip(): value.strip() for key, value in fields.items()}
    if fields.get("Leap status") != "Normal" or int(fields["Stratum"]) <= 0:
        raise RuntimeError("chrony is not synchronized")
    reference_id = fields["Reference ID"].split()[0].upper()
    expected = ("7F7F0101" if plan["node_id"] == plan["reference_node"] else
                ipaddress.IPv4Address(plan["reference_address"]).packed.hex().upper())
    if reference_id != expected:
        raise RuntimeError(f"chrony reference {reference_id} does not match configured source {expected}")
    remaining = abs(float(fields["System time"].split()[0]))
    last_offset = abs(float(fields["Last offset"].split()[0]))
    if not all(math.isfinite(value) and value <= plan["clock_max_offset_seconds"]
               for value in (remaining, last_offset)):
        raise RuntimeError("chrony offset exceeds the configured readiness threshold")
    if plan["node_id"] != plan["reference_node"]:
        sample = calendar.timegm(datetime.strptime(fields["Ref time (UTC)"], "%a %b %d %H:%M:%S %Y").timetuple())
        age = (time.time() if now is None else now) - sample
        if age < -1 or age > plan["clock_max_age_seconds"]:
            raise RuntimeError(f"chrony reference sample age {age:.3f}s exceeds readiness threshold")


def check(plan: dict) -> None:
    """Print local interface and clock reports without changing configuration."""
    if not plan["clock_managed"]:
        print("Fake backend uses software timing; no physical network or chrony check required" if plan["backend"] == "fake"
              else "Single-node group uses local timing; no network or chrony check required")
        return
    report = _run(["ip", "-j", "-4", "address", "show", "dev", plan["interface"]], capture=True)
    addresses = {address["local"] for interface in json.loads(report)
                 for address in interface["addr_info"]}
    if plan["node_address"] not in addresses:
        raise RuntimeError(f"Interface {plan['interface']} does not have address {plan['node_address']}")
    print(f"Interface {plan['interface']} has configured address {plan['node_address']}")
    if plan["clock_managed"]:
        report = _run(["chronyc", "-n", "tracking"], capture=True)
        print(report)
        validate_tracking(plan, report)
        _run(["chronyc", "-n", "sources"])
        print("Clock is ready for capture")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "prepare", "check", "clock-prepare", "restore"])
    parser.add_argument("config", type=Path)
    parser.add_argument("--node", required=True)
    parser.add_argument("--app-user", help="Local application account allowed to run the fixed clock helper")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text())
        plan = make_plan(config, args.node, args.app_user)
        if args.action == "plan":
            print(json.dumps(plan, indent=2))
        elif args.action == "prepare":
            prepare(plan)
        elif args.action == "restore":
            restore(plan)
        elif args.action == "check":
            check(plan)
        elif plan["clock_prepare_command"]:
            _run(plan["clock_prepare_command"], timeout_seconds=float(config.get("startup_timeout_seconds", 60)) + 10)
        else:
            print("Fake backend uses software timing; no clock preparation required" if plan["backend"] == "fake"
                  else "Single-node group uses the local clock; no clock preparation required")
    except (ValueError, KeyError, StopIteration, OSError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"bcamsync node setup: {error}\n")


if __name__ == "__main__":
    main()
