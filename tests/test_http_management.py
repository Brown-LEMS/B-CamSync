"""HTTP attaches to caller-owned sessions without camera or network ownership."""

from contextlib import ExitStack, closing
import http.client
import json
from pathlib import Path
import socket
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from bcamsync import Config, FrameTiming, NetworkSession, Session, SyncConfig, SyncSession, SyncUnit, TimingSnapshot, boot_time_ns
from bcamsync.http_management import HttpNodeService
from bcamsync.management import NodeService
from bcamsync_host import HttpGroupClient, HttpNodeTarget, discover


ROOT = Path(__file__).resolve().parents[1]


def configuration(name="fake_one_node.json"):
    config = Config.from_file(ROOT / "examples/configs" / name)
    nodes = config.nodes
    with ExitStack() as stack:
        for node in nodes:
            channel = stack.enter_context(socket.socket())
            channel.bind(("127.0.0.1", 0))
            node.port = channel.getsockname()[1]
    config.nodes = nodes
    return config


def udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
        channel.bind(("127.0.0.1", 0))
        return channel.getsockname()[1]


class RunningService:
    def __init__(self, service):
        self.service = service
        self.errors = []
        self.thread = threading.Thread(target=self.run)

    def run(self):
        try:
            self.service.serve()
        except BaseException as error:
            self.errors.append(error)

    def __enter__(self):
        self.thread.start()
        if not self.service.listening.wait(5):
            self.service.request_shutdown()
            self.thread.join(5)
            raise RuntimeError(f"HTTP service failed to listen: {self.errors}")
        return self.service

    def __exit__(self, *args):
        self.service.request_shutdown()
        self.thread.join(10)
        if self.thread.is_alive() or self.errors:
            raise AssertionError(f"HTTP service failed to close: {self.errors}")


def request(service, method, path, body=None, headers=None):
    with closing(http.client.HTTPConnection(service.address, service.http_port, timeout=5)) as client:
        client.request(method, path, body=body, headers=headers or {})
        response = client.getresponse()
        return response.status, json.loads(response.read())


class HttpManagementTests(unittest.TestCase):
    def test_discovery_before_capture_and_transport_shutdown_preserves_session(self):
        config = configuration()
        session = Session(config, "node-a")
        controller = NodeService(config, "node-a", session=session)
        self.addCleanup(controller.close)
        with patch.object(controller, "_create_session", side_effect=AssertionError("capture factory called")):
            with RunningService(HttpNodeService(controller, "127.0.0.1", http_port=0, discovery_port=udp_port())) as service:
                targets = discover(config.group_id, .6, "127.0.0.1", service.discovery_port)
                self.assertEqual(targets, {"node-a": HttpNodeTarget(f"http://127.0.0.1:{service.http_port}")})
                client = HttpGroupClient(json.loads(config.to_json()), targets)
                self.assertTrue(client.check()["sync_contract_matches"])
                self.assertIs(service.session, session)
                self.assertFalse(controller.request("status")["active"])
                client.start()
                self.assertTrue(session.wait_ready(30))
                packet = session.read("board-a1", 1)
                self.assertIsNotNone(packet)
                with self.assertRaisesRegex(RuntimeError, "stop the session"):
                    session.set_node_addresses({"node-a": "127.0.0.1"})
                client.stop()
                self.assertFalse(client.status()["group"]["active"])
                with self.assertRaises(ValueError):
                    session.set_node_addresses({"node-a": "invalid"})
                self.assertEqual(discover(config.group_id, .5, "127.0.0.1", service.discovery_port), targets)
                client.start()
                self.assertIs(controller.session, session)
                self.assertTrue(session.wait_ready(30))
            # HTTP shutdown does not close the supplied controller or capture.
            self.assertTrue(controller.request("status")["active"])
            self.assertIsNotNone(session.read("board-a2", 1))

    def test_attach_to_already_running_application_session(self):
        config = configuration()
        session = Session(config, "node-a")
        session.start()
        controller = NodeService(config, "node-a", session=session, active=True)
        self.addCleanup(controller.close)
        with RunningService(HttpNodeService(controller, "127.0.0.1", http_port=0, discovery_port=udp_port())) as service:
            self.assertTrue(request(service, "GET", "/status")[1]["result"]["active"])
            self.assertEqual(request(service, "POST", "/start", b"{}")[0], 409)
            first = session.read("board-a1", 1)
            self.assertIsNotNone(first)
        following = session.read("board-a1", 1)
        self.assertIsNotNone(following)
        self.assertGreater(following.sequence, first.sequence)

    def test_management_address_does_not_replace_synchronization_endpoints(self):
        config = configuration("fake_two_nodes.json")
        port = udp_port()
        controllers = {}
        with ExitStack() as stack:
            for index, node in enumerate(config.nodes):
                session = Session(config, node.id)
                controller = NodeService(config, node.id, session=session)
                controllers[node.id] = controller
                self.addCleanup(controller.close)
                stack.enter_context(RunningService(HttpNodeService(controller, f"127.0.0.{index + 2}",
                                                    http_port=0, discovery_port=port)))
            targets = discover(config.group_id, .8, "127.0.0.1", port)
            self.assertEqual(set(targets), {"node-a", "node-b"})
            self.assertTrue(targets["node-a"].base_url.startswith("http://127.0.0.2:"))
            self.assertTrue(targets["node-b"].base_url.startswith("http://127.0.0.3:"))
            client = HttpGroupClient(json.loads(config.to_json()), targets)
            client.start()
            self.assertTrue(controllers["node-a"].session.wait_ready(30))
            self.assertIsNotNone(controllers["node-b"].session.read("board-b1", 1))
            self.assertTrue(all(node.address == "127.0.0.1" for node in config.nodes))
            client.stop()

    def test_invalid_http_requests_do_not_create_capture(self):
        controller = NodeService(configuration(), "node-a")
        self.addCleanup(controller.close)
        with RunningService(HttpNodeService(controller, "127.0.0.1", http_port=0, discovery_port=udp_port())) as service:
            for method, path, body, headers, expected in [
                ("GET", "/start", None, {}, 404),
                ("POST", "/status", None, {}, 404),
                ("POST", "/start", b"[1]", {}, 400),
                ("POST", "/start", b"{}", {"Content-Length": "1048577"}, 400),
                ("POST", "/start", b'{"unexpected":1}', {}, 400),
                ("POST", "/start", b'{"operation_id":"wrong"}', {}, 400),
            ]:
                with self.subTest(method=method, body=body):
                    code, result = request(service, method, path, body, headers)
                    self.assertEqual(code, expected)
                    self.assertFalse(result["ok"])
            self.assertIsNone(controller.session)

    def test_command_identity_prevents_stopping_another_operation(self):
        controller = NodeService(configuration(), "node-a")
        self.addCleanup(controller.close)
        with RunningService(HttpNodeService(controller, "127.0.0.1", http_port=0, discovery_port=udp_port())) as service:
            owner = str(uuid4())
            self.assertEqual(request(service, "POST", "/start", json.dumps({"operation_id": owner}))[0], 200)
            code, result = request(service, "POST", "/stop", json.dumps({"operation_id": str(uuid4())}))
            self.assertEqual(code, 409)
            self.assertFalse(result["ok"])
            self.assertTrue(controller.request("status")["active"])
            self.assertEqual(request(service, "POST", "/stop", json.dumps({"operation_id": owner}))[0], 200)

    def test_shutdown_during_session_creation_does_not_start_capture(self):
        controller = NodeService(configuration(), "node-a")
        entered, release = threading.Event(), threading.Event()
        from unittest.mock import Mock
        session = Mock()
        errors = []

        def create():
            entered.set()
            release.wait(3)
            return session

        def start():
            try:
                controller.request("start")
            except RuntimeError as error:
                errors.append(str(error))

        with patch.object(controller, "_create_session", create):
            thread = threading.Thread(target=start)
            thread.start()
            self.assertTrue(entered.wait(3))
            controller.request_shutdown()
            release.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
        session.start.assert_not_called()
        self.assertEqual(errors, ["node service is closing"])
        controller.close()

    def test_failed_http_bind_does_not_close_supplied_controller(self):
        controller = NodeService(configuration(), "node-a")
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            service = HttpNodeService(controller, "127.0.0.1", http_port=occupied.getsockname()[1],
                                      discovery_port=udp_port())
            with self.assertRaises(OSError):
                service.serve()
            self.assertFalse(service.listening.is_set())
            self.assertFalse(controller.request("status")["active"])
        controller.close()

    def test_discovery_thread_start_failure_closes_transport_only(self):
        controller = NodeService(configuration(), "node-a")
        with patch("bcamsync.http_management._Discovery.start", side_effect=RuntimeError("thread start failed")):
            service = HttpNodeService(controller, "127.0.0.1", http_port=0, discovery_port=udp_port())
            with self.assertRaisesRegex(RuntimeError, "thread start failed"):
                service.serve()
            self.assertEqual(service._discovery.channel.fileno(), -1)
            self.assertEqual(service._discovery.sender.fileno(), -1)
            self.assertFalse(controller.request("status")["active"])
        controller.close()

    def test_external_session_control_never_owns_existing_capture(self):
        class Capture(SyncUnit):
            running = True
            attached = False

            def bind(self, session_id, round_id):
                self.attached = True

            def snapshot(self):
                value = TimingSnapshot()
                value.unit_id = "sensor"
                value.snapshot_timestamp_ns = boot_time_ns()
                frame = FrameTiming()
                frame.camera_sequence = 1
                frame.sensor_timestamp_ns = frame.timestamp_ns = value.snapshot_timestamp_ns - 1
                value.frames = [frame]
                return value

            def adjust(self, pulse):
                raise AssertionError("reference cannot receive corrections")

            def cancel(self):
                self.attached = False

            def fault(self):
                return ""

        sync = configuration().sync_config()
        sync.units = [sync.units[0]]
        units = sync.units
        units[0].id = "sensor"
        sync.units = units
        sync.reference_unit = "sensor"
        capture = Capture()
        session = SyncSession(sync, "node-a", {"sensor": capture})
        network = NetworkSession(sync, "node-a")
        network.start(1)
        self.addCleanup(network.stop)
        controller = NodeService(sync, "node-a", session=session, network=network)
        self.addCleanup(controller.close)
        with RunningService(HttpNodeService(controller, "127.0.0.1", http_port=0, discovery_port=udp_port())) as service:
            self.assertEqual(request(service, "POST", "/start", b"{}")[0], 200)
            self.assertTrue(session.wait_ready(2))
        self.assertTrue(capture.running)
        self.assertTrue(capture.attached)
        controller.request("stop")
        self.assertTrue(capture.running)
        self.assertFalse(capture.attached)
        self.assertEqual(network.address, "127.0.0.1")
        controller.request("start")
        self.assertTrue(session.wait_ready(2))
        controller.close()
        self.assertTrue(capture.running)
        self.assertFalse(capture.attached)
        self.assertEqual(network.address, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
