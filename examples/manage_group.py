"""Manage discovered HTTP node services from a developer host.

An optional --ssh-targets file selects the existing SSH transport instead.
"""

import argparse
import json
from pathlib import Path

from bcamsync_host import GroupClient, HttpGroupClient, NodeTarget, discover


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("command", choices=["discover", "check", "start", "status", "stop"])
    parser.add_argument("--ssh-targets", type=Path)
    network = parser.add_mutually_exclusive_group()
    network.add_argument("--address", default="0.0.0.0", help="local outgoing IPv4 address")
    network.add_argument("--interface", help="local network interface, such as en7 or eth0")
    parser.add_argument("--discovery-port", type=int, default=58082)
    parser.add_argument("--discovery-timeout", type=float, default=2)
    args = parser.parse_args()
    if args.command == "discover" and args.ssh_targets:
        parser.error("discover uses HTTP service discovery; omit --ssh-targets")
    config = json.loads(args.config.read_text())
    if args.ssh_targets:
        targets = {node_id: NodeTarget(**target) for node_id, target in json.loads(args.ssh_targets.read_text()).items()}
        client = GroupClient(config, targets)
    else:
        targets = discover(config["group_id"], args.discovery_timeout, args.address,
                           args.discovery_port, interface=args.interface)
        if args.command == "discover":
            print(json.dumps({node_id: {"base_url": target.base_url}
                              for node_id, target in sorted(targets.items())}, indent=2))
            return
        client = HttpGroupClient(config, targets)
    result = getattr(client, args.command)()
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
