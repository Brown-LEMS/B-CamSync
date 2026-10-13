"""Manage preconfigured node services from an optional developer host.

TARGETS is a JSON object mapping each node ID to ssh_host, python, and socket.
The remote interpreter should be the node's installed B-CamSync environment.
"""

import argparse
import json
from pathlib import Path

from bcamsync import Config
from bcamsync.management import GroupClient, NodeTarget


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("targets", type=Path)
    parser.add_argument("command", choices=["check", "start", "status", "stop"])
    args = parser.parse_args()
    targets = {node_id: NodeTarget(**target) for node_id, target in json.loads(args.targets.read_text()).items()}
    client = GroupClient(Config.from_file(args.config), targets)
    result = getattr(client, args.command)()
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
