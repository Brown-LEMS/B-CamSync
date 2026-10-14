"""Host-only management without importing the node package or camera dependencies."""

from copy import deepcopy
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
from unittest.mock import patch

from bcamsync_host import GroupClient, NodeTarget


ROOT = Path(__file__).resolve().parents[1]


def configuration():
    return {"group_id": "lab", "reference_unit": "board-a", "nodes": [
                {"id": "node-b", "address": "10.77.0.2"},
                {"id": "node-a", "address": "10.77.0.1"}],
            "units": [{"id": "board-b", "node_id": "node-b", "queue_capacity": 4},
                      {"id": "board-a", "node_id": "node-a", "exposure_us": 10000}]}


class HostClientTests(unittest.TestCase):
    def setUp(self):
        self.config = configuration()
        self.targets = {node: NodeTarget(node, "/opt/camera env/bin/python", "/run/app camera.sock")
                        for node in ("node-a", "node-b")}
        self.client = GroupClient(self.config, self.targets)
        self.contract = {"group_id": "lab", "reference_unit": "board-a", "clock_mode": "network",
                         "fps": 40, "clock_domain": "common", "nodes": ["node-a", "node-b"],
                         "units": [{"id": "board-a", "node_id": "node-a"},
                                   {"id": "board-b", "node_id": "node-b"}]}

    def report(self, node_id):
        return {"node_id": node_id, "config": deepcopy(self.config),
                "sync_contract": deepcopy(self.contract), "reference_node": "node-a"}

    def test_host_import_and_status_with_site_packages_disabled(self):
        script = '''
import importlib.util
import json
import sys
from unittest.mock import patch
from bcamsync_host import GroupClient, NodeTarget
assert importlib.util.find_spec("numpy") is None
assert importlib.util.find_spec("bcamsync") is None
config = json.loads(sys.argv[1])
targets = {node["id"]: NodeTarget(node["id"], "/opt/venv/bin/python", "/run/node.sock")
           for node in config["nodes"]}
client = GroupClient(config, targets)
with patch.object(client, "_request", side_effect=lambda node, command: {"node_id": node}):
    assert client.status()["reference_node"] == "node-a"
assert "numpy" not in sys.modules
assert "bcamsync" not in sys.modules
'''
        completed = subprocess.run([sys.executable, "-S", "-c", script, json.dumps(self.config)],
                                   env={**os.environ, "PYTHONPATH": str(ROOT / "host")},
                                   capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_ssh_preserves_interpreter_and_socket_as_single_arguments(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps({"node_id": "node-a"}), "")
        with patch("bcamsync_host.subprocess.run", return_value=completed) as run:
            self.client._request("node-a", "start", "request-token")
        args, kwargs = run.call_args
        self.assertEqual(args[0][:-1], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "node-a"])
        self.assertEqual(shlex.split(args[0][-1]), ["/opt/camera env/bin/python", "-m",
                         "bcamsync.management", "request", "start", "--socket", "/run/app camera.sock",
                         "--timeout", "80", "--operation-id", "request-token"])
        self.assertEqual(kwargs["timeout"], 90)
        self.assertFalse(kwargs.get("shell", False))

    def test_ssh_failure_reports_node_command_and_remote_error(self):
        completed = subprocess.CompletedProcess([], 1, "", "camera session failed\n")
        with patch("bcamsync_host.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "Node node-a start failed: camera session failed"):
                self.client._request("node-a", "start")

    def test_ssh_wrong_node_identity_is_rejected(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps({"node_id": "node-b"}), "")
        with patch("bcamsync_host.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "node identity"):
                self.client._request("node-a", "status")

    def test_local_capture_and_queue_differences_do_not_block_check(self):
        def request(node_id, command):
            self.assertEqual(command, "configuration")
            report = self.report(node_id)
            report["config"]["units"][0]["queue_capacity"] = 10
            report["config"]["units"][1]["exposure_us"] = 2000
            report["config"]["units"][1]["analogue_gain"] = 2
            report["config"]["nodes"].reverse()
            return report
        with patch.object(self.client, "_request", side_effect=request):
            self.assertTrue(self.client.check()["sync_contract_matches"])

    def test_shared_membership_reference_and_fps_mismatch_block_start(self):
        changes = {"nodes": ["node-a", "node-c"], "reference_unit": "board-b", "fps": 41,
                   "clock_mode": "host",
                   "units": [{"id": "board-a", "node_id": "node-a"},
                             {"id": "board-b", "node_id": "node-a"}]}
        for field, value in changes.items():
            with self.subTest(field=field):
                def request(node_id, command):
                    report = self.report(node_id)
                    report["sync_contract"][field] = value
                    return report
                with patch.object(self.client, "_request", side_effect=request) as call:
                    with self.assertRaisesRegex(RuntimeError, "Synchronization contract differs"):
                        self.client.start()
                self.assertTrue(all(args[1] == "configuration" for args, _ in call.call_args_list))

    def test_standalone_sync_configuration_has_no_acquisition_contract(self):
        independent = deepcopy(self.config)
        independent["clock_mode"] = "host"
        independent["units"] = [{"id": unit["id"], "node_id": unit["node_id"]}
                                for unit in independent["units"]]
        client = GroupClient(independent, self.targets)
        self.assertEqual(client.sync_contract["clock_mode"], "host")
        self.assertNotIn("backend", client.sync_contract)
        managed = deepcopy(self.config)
        managed["backend"] = "fake"
        self.assertEqual(GroupClient(managed, self.targets).sync_contract, client.sync_contract)
        managed["clock_mode"] = "network"
        self.assertEqual(GroupClient(managed, self.targets).sync_contract["clock_mode"], "network")
        independent["clock_mode"] = "unknown"
        with self.assertRaisesRegex(ValueError, "clock_mode"):
            GroupClient(independent, self.targets)

    def test_target_membership_must_cover_configured_nodes(self):
        with self.assertRaisesRegex(ValueError, "every configured node"):
            GroupClient(self.config, {"node-a": self.targets["node-a"]})

    def test_stop_attempts_all_nodes_and_reports_failures(self):
        def request(node_id, command):
            if node_id == "node-a":
                raise OSError("SSH disconnected")
            return {"node_id": node_id, "active": False}
        with patch.object(self.client, "_request", side_effect=request) as call:
            with self.assertRaisesRegex(RuntimeError, "node-a.*SSH disconnected"):
                self.client.stop()
        self.assertEqual([args[0] for args, _ in call.call_args_list], ["node-a", "node-b"])


if __name__ == "__main__":
    unittest.main()
