"""Optional management lifecycle through real local services and fake cameras."""

from contextlib import ExitStack
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import numpy as np

from bcamsync import Config
from bcamsync.management import NodeService, request_local
from bcamsync_host import GroupClient, NodeTarget


ROOT = Path(__file__).resolve().parents[1]


def configuration(name: str):
    config = Config.from_file(ROOT / "tests" / "fixtures" / "configs" / name)
    nodes = config.nodes
    with ExitStack() as reservations:
        for node in nodes:
            endpoint = reservations.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            endpoint.bind(("127.0.0.1", 0))
            node.port = endpoint.getsockname()[1]
        config.nodes = nodes
    # Management checks lifecycle, while the dedicated fake tests exercise
    # initial phase correction and drift. Aligned boards shorten repeated starts.
    units = config.units
    for unit in units:
        fake = unit.fake
        fake.initial_phase_us = 0
        fake.drift_ppm = 0
        unit.fake = fake
    config.units = units
    config.validate()
    return config


class LocalServiceTests(unittest.TestCase):
    def test_explicit_acquisition_outlives_clients_and_reuses_session_on_restart(self):
        config = configuration("fake_one_node.json")
        with tempfile.TemporaryDirectory(prefix="bcamsync-management-") as directory:
            path = Path(directory) / "node.sock"
            service = NodeService(config, "node-a", path)
            failures = []

            def serve():
                try:
                    service.serve()
                except BaseException as error:
                    failures.append(error)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not path.exists():
                    if failures:
                        raise failures[0]
                    if time.monotonic() >= deadline:
                        self.fail("Node service did not bind its Unix socket")
                    time.sleep(0.01)
                report = request_local(path, "configuration", timeout_seconds=5)
                self.assertEqual(report["node_id"], "node-a")
                self.assertEqual(report["config"], json.loads(config.to_json()))
                self.assertEqual(report["sync_contract"], json.loads(config.sync_json()))
                self.assertEqual(report["reference_node"], config.reference_node())
                self.assertIsNone(service.session)
                idle = request_local(path, "status", timeout_seconds=5)
                self.assertFalse(idle["active"])
                self.assertEqual((idle["waiting_nodes"], idle["stop_node"], idle["stop_reason"]), ([], "", ""))
                owner = str(uuid4())
                request_local(path, "start", timeout_seconds=5, operation_id=owner)
                session = service.session
                self.assertIsNotNone(session)
                self.assertTrue(session.wait_ready(45))
                # request_local has already closed its client connection. Native
                # capture and synchronization still run, without a host connection.
                packet = session.read("board-a1", 1)
                self.assertIsNotNone(packet)
                fragment = packet.view(1)[::2, 1:]
                expected = fragment.copy()
                self.assertEqual(fragment.dtype, np.uint8)
                next_packet = session.read("board-a1", 1)
                self.assertIsNotNone(next_packet)
                self.assertGreater(next_packet.sequence, packet.sequence)
                self.assertTrue(request_local(path, "status", timeout_seconds=5)["active"])
                with self.assertRaisesRegex(RuntimeError, "different management operation"):
                    request_local(path, "stop", timeout_seconds=5, operation_id=str(uuid4()))
                self.assertTrue(request_local(path, "status", timeout_seconds=5)["active"])
                closed = request_local(path, "stop", timeout_seconds=5, operation_id=owner)
                self.assertFalse(closed["active"])
                self.assertEqual(closed["state"], "stopped")
                self.assertEqual(closed["fault"], "")
                native = session.status()
                self.assertEqual((closed["waiting_nodes"], closed["stop_node"], closed["stop_reason"]),
                                 (native.waiting_nodes, native.stop_node, native.stop_reason))
                request_local(path, "start", timeout_seconds=5)
                self.assertIs(service.session, session)
                self.assertTrue(session.wait_ready(45))
                self.assertIsNotNone(session.read("board-a2", 1))
                np.testing.assert_array_equal(fragment, expected)
            finally:
                service.request_shutdown()
                thread.join(timeout=15)
            self.assertFalse(thread.is_alive(), "Service shutdown did not complete")
            self.assertEqual(failures, [])
            self.assertFalse(path.exists(), "Service left its Unix socket behind")
            self.assertFalse(session.status().ready)
            self.assertEqual(session.status().fault, "")
            np.testing.assert_array_equal(fragment, expected)
            # A fresh idle service can bind the same path after shutdown.
            replacement = NodeService(config, "node-a", path)
            replacement_failures = []

            def serve_replacement():
                try:
                    replacement.serve()
                except BaseException as error:
                    replacement_failures.append(error)

            replacement_thread = threading.Thread(target=serve_replacement, daemon=True)
            replacement_thread.start()
            try:
                deadline = time.monotonic() + 5
                while not path.exists():
                    if replacement_failures:
                        raise replacement_failures[0]
                    if time.monotonic() >= deadline:
                        self.fail("Replacement service did not bind the released socket")
                    time.sleep(0.01)
                self.assertFalse(request_local(path, "status", timeout_seconds=5)["active"])
                self.assertIsNone(replacement.session)
            finally:
                replacement.request_shutdown()
                replacement_thread.join(timeout=5)
            self.assertFalse(replacement_thread.is_alive())
            self.assertEqual(replacement_failures, [])
            self.assertFalse(path.exists())


class GroupManagementTests(unittest.TestCase):
    def setUp(self):
        self.config = configuration("fake_two_nodes.json")
        self.directory = tempfile.TemporaryDirectory(prefix="bcamsync-group-")
        self.services = {node.id: NodeService(self.config, node.id, Path(self.directory.name) / f"{node.id}.sock")
                         for node in self.config.nodes}
        targets = {node.id: NodeTarget(f"{node.id}.invalid", "/opt/bcamsync/.venv/bin/python",
                                      str(Path(self.directory.name) / f"{node.id}.sock"))
                   for node in self.config.nodes}
        self.client = GroupClient(json.loads(self.config.to_json()), targets)
        self.calls = []

    def tearDown(self):
        # Stop the reference first, even if a test failed partway through startup.
        self.services[self.config.reference_node()].close()
        for node_id, service in self.services.items():
            if node_id != self.config.reference_node():
                service.close()
        self.directory.cleanup()

    def local_request(self, node_id, command, operation_id=None):
        self.calls.append((node_id, command, operation_id))
        return self.services[node_id].request(command, operation_id)

    def test_start_and_stop_order_and_group_status(self):
        with patch.object(self.client, "_request", side_effect=self.local_request):
            started = self.client.start()
            self.assertEqual([node for node, command, _ in self.calls if command == "start"], ["node-b", "node-a"])
            self.assertTrue(all(node["active"] for node in started["nodes"].values()))
            status = self.client.status()
            self.assertEqual(status["reference_node"], "node-a")
            self.assertEqual(status["group"]["node_id"], "node-a")
            self.assertEqual(set(status["nodes"]), {"node-a", "node-b"})
            self.calls.clear()
            stopped = self.client.stop()
        self.assertEqual([node for node, command, _ in self.calls if command == "stop"], ["node-a", "node-b"])
        self.assertTrue(all(not node["active"] for node in stopped["nodes"].values()))

    def test_configuration_mismatch_is_rejected_before_any_acquisition(self):
        differing = configuration("fake_two_nodes.json")
        differing.group_id = "different-group"
        self.services["node-b"] = NodeService(differing, "node-b", Path(self.directory.name) / "node-b.sock")
        with patch.object(self.client, "_request", side_effect=self.local_request):
            with self.assertRaisesRegex(RuntimeError, "contract differs"):
                self.client.start()
        self.assertTrue(all(command == "configuration" for _, command, _ in self.calls))
        self.assertTrue(all(service.session is None for service in self.services.values()))

    def test_local_capture_settings_can_differ_with_matching_sync_contract(self):
        differing = configuration("fake_two_nodes.json")
        units = differing.units
        units[0].queue_capacity = 5
        units[0].exposure_us = 2000
        units[1].analogue_gain = 2
        differing.units = units
        self.services["node-b"] = NodeService(differing, "node-b", Path(self.directory.name) / "node-b.sock")
        with patch.object(self.client, "_request", side_effect=self.local_request):
            report = self.client.check()
        self.assertTrue(report["sync_contract_matches"])
        self.assertNotEqual(report["nodes"]["node-a"]["config"], report["nodes"]["node-b"]["config"])
        self.assertTrue(all(service.session is None for service in self.services.values()))

    def test_active_node_is_not_restarted_or_stopped_by_new_start(self):
        self.services["node-a"].request("start", str(uuid4()))
        existing = self.services["node-a"].session
        with patch.object(self.client, "_request", side_effect=self.local_request):
            with self.assertRaisesRegex(RuntimeError, "already active"):
                self.client.start()
        self.assertIs(self.services["node-a"].session, existing)
        self.assertTrue(self.services["node-a"].request("status")["active"])
        self.assertFalse(any(command in {"start", "stop"} for _, command, _ in self.calls))

    def test_failed_start_cleanup_preserves_another_operations_session(self):
        other_owner = str(uuid4())

        def competing_start(node_id, command, operation_id=None):
            if node_id == "node-a" and command == "start":
                self.services[node_id].request("start", other_owner)
            return self.local_request(node_id, command, operation_id)

        with patch.object(self.client, "_request", side_effect=competing_start):
            with self.assertRaisesRegex(RuntimeError, "Group start failed"):
                self.client.start()
        reference = self.services["node-a"].request("status")
        self.assertTrue(reference["active"])
        self.assertEqual(reference["operation_id"], other_owner)
        self.assertFalse(self.services["node-b"].request("status")["active"])
        start_operations = {operation for _, command, operation in self.calls if command == "start"}
        cleanup_operations = {operation for _, command, operation in self.calls if command == "stop"}
        self.assertEqual(start_operations, cleanup_operations)
        self.assertNotIn(other_owner, cleanup_operations)

    def test_lost_start_acknowledgement_still_cleans_up_owned_session(self):
        def dropped_response(node_id, command, operation_id=None):
            result = self.local_request(node_id, command, operation_id)
            if node_id == "node-b" and command == "start":
                raise OSError("connection lost after starting follower")
            return result

        with patch.object(self.client, "_request", side_effect=dropped_response):
            with self.assertRaisesRegex(RuntimeError, "Group start failed"):
                self.client.start()
        self.assertFalse(self.services["node-b"].request("status")["active"])
        self.assertIsNone(self.services["node-a"].session)


if __name__ == "__main__":
    unittest.main()
