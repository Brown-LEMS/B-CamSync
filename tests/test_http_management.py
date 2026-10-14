"""Real HTTP/UDP management with fake capture and explicit network ownership."""

from contextlib import ExitStack, closing
import http.client
import json
from pathlib import Path
import socket
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from bcamsync import Config
from bcamsync.http_management import HttpNodeService
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
    def test_discovery_before_capture_and_control_after_stop(self):
        config = configuration()
        with RunningService(HttpNodeService(config, "node-a", http_port=0, discovery_port=udp_port())) as service:
            targets = discover(config.group_id, .7, "127.0.0.1", service.discovery_port)
            self.assertEqual(targets, {"node-a": HttpNodeTarget(f"http://127.0.0.1:{service.http_port}")})
            client = HttpGroupClient(json.loads(config.to_json()), targets)
            self.assertTrue(client.check()["sync_contract_matches"])
            self.assertIsNone(service.session)
            client.start()
            self.assertTrue(service.session.wait_ready(30))
            packet = service.session.read("board-a1", 1)
            self.assertIsNotNone(packet)
            first = service.session
            with self.assertRaisesRegex(RuntimeError, "stop the session"):
                first.set_node_addresses({"node-a": "127.0.0.1"})
            # Closing a connection is not a stop instruction.
            request(service, "GET", "/status")
            self.assertTrue(service.request("status")["active"])
            client.stop()
            self.assertFalse(client.status()["group"]["active"])
            with self.assertRaisesRegex(ValueError, "every configured node"):
                first.set_node_addresses({})
            with self.assertRaises(ValueError):
                first.set_node_addresses({"node-a": "invalid"})
            self.assertTrue(service.listening.is_set())
            self.assertEqual(discover(config.group_id, .6, "127.0.0.1", service.discovery_port), targets)
            client.start()
            self.assertIs(first, service.session)
            self.assertTrue(service.session.wait_ready(30))
            self.assertIsNotNone(service.session.read("board-a2", 1))
            client.stop()
        self.assertEqual(service.session.status().state, "stopped")

    def test_invalid_http_requests_do_not_start_capture(self):
        with RunningService(HttpNodeService(configuration(), "node-a", http_port=0, discovery_port=udp_port())) as service:
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
            self.assertIsNone(service.session)
            code, _ = request(service, "GET", "/status")
            self.assertEqual(code, 200)

    def test_command_identity_prevents_stopping_another_operation(self):
        with RunningService(HttpNodeService(configuration(), "node-a", http_port=0, discovery_port=udp_port())) as service:
            owner = str(uuid4())
            code, _ = request(service, "POST", "/start", json.dumps({"operation_id": owner}))
            self.assertEqual(code, 200)
            code, result = request(service, "POST", "/stop", json.dumps({"operation_id": str(uuid4())}))
            self.assertEqual(code, 409)
            self.assertFalse(result["ok"])
            self.assertTrue(service.request("status")["active"])
            code, _ = request(service, "POST", "/stop", json.dumps({"operation_id": owner}))
            self.assertEqual(code, 200)

    def test_auto_service_holds_network_across_group_restart(self):
        config = configuration("fake_two_nodes.json")
        config.network_mode = "auto"
        nodes = config.nodes
        for node in nodes:
            node.interface = "eth0"
        config.nodes = nodes
        links = []

        class Link:
            def __init__(self, interface, identity):
                self.interface, self.identity, self.starts, self.stops = interface, identity, 0, 0
                links.append(self)

            def start(self, timeout):
                self.starts += 1
                return "127.0.0.1"

            def stop(self):
                self.stops += 1

        port = udp_port()
        with patch("bcamsync.http_management._NetworkLink", Link):
            with ExitStack() as stack:
                services = {node.id: stack.enter_context(RunningService(HttpNodeService(config, node.id,
                            http_port=0, discovery_port=port))) for node in nodes}
                targets = discover(config.group_id, 1, "127.0.0.1", port)
                self.assertEqual(set(targets), {"node-a", "node-b"})
                client = HttpGroupClient(json.loads(config.to_json()), targets)
                sessions = None
                for iteration in range(2):
                    if iteration:
                        # Simulate a peer rejoining with a different local address.
                        changed = {"node-a": "127.0.0.1", "node-b": "127.0.0.2"}
                        for service in services.values():
                            service._discovery.addresses = lambda: changed
                    client.start()
                    if sessions is None:
                        sessions = {node: service.session for node, service in services.items()}
                    else:
                        self.assertTrue(all(service.session is sessions[node] for node, service in services.items()))
                    self.assertTrue(services["node-a"].session.wait_ready(30))
                    self.assertIsNotNone(services["node-b"].session.read("board-b1", 1))
                    client.stop()
                    self.assertTrue(all(link.starts == 1 and link.stops == 0 for link in links))
                    self.assertTrue(all(service.listening.is_set() for service in services.values()))
                for service in services.values():
                    with patch.object(service._discovery, "addresses", side_effect=RuntimeError("peer absent")) as lookup:
                        code, _ = request(service, "POST", "/start", b'{"operation_id":"wrong"}')
                        self.assertEqual(code, 400)
                        lookup.assert_not_called()
                self.assertEqual(config.network_mode, "auto")
                self.assertTrue(all(node.address == "127.0.0.1" for node in config.nodes))
            self.assertTrue(all(link.stops == 1 for link in links))

    def test_shutdown_rejects_queued_start_without_acquiring_cameras(self):
        service = HttpNodeService(configuration(), "node-a", http_port=0, discovery_port=udp_port())
        service.request_shutdown()
        with self.assertRaisesRegex(RuntimeError, "closing"):
            service.request("start")
        self.assertIsNone(service.session)

    def test_shutdown_during_session_creation_does_not_start_capture(self):
        service = HttpNodeService(configuration(), "node-a", http_port=0, discovery_port=udp_port())
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
                service.request("start")
            except RuntimeError as error:
                errors.append(str(error))

        with patch.object(service, "_create_session", create):
            thread = threading.Thread(target=start)
            thread.start()
            self.assertTrue(entered.wait(3))
            service.request_shutdown()
            release.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
        session.start.assert_not_called()
        self.assertEqual(errors, ["node service is closing"])
        service.close()

    def test_discovery_thread_start_failure_releases_network(self):
        config = configuration()
        config.network_mode = "auto"
        nodes = config.nodes
        nodes[0].interface = "eth0"
        config.nodes = nodes
        with patch("bcamsync.http_management._NetworkLink") as factory, \
                patch("bcamsync.http_management._Discovery.start", side_effect=RuntimeError("thread start failed")):
            factory.return_value.start.return_value = "127.0.0.1"
            service = HttpNodeService(config, "node-a", http_port=0, discovery_port=udp_port())
            with self.assertRaisesRegex(RuntimeError, "thread start failed"):
                service.serve()
            self.assertEqual(service._discovery.channel.fileno(), -1)
            factory.return_value.stop.assert_called_once()

    def test_failed_capture_cleanup_still_releases_discovery_and_network(self):
        config = configuration()
        config.network_mode = "auto"
        nodes = config.nodes
        nodes[0].interface = "eth0"
        config.nodes = nodes
        with patch("bcamsync.http_management._NetworkLink") as factory:
            factory.return_value.start.return_value = "127.0.0.1"
            service = HttpNodeService(config, "node-a", http_port=0, discovery_port=udp_port())
            from unittest.mock import Mock
            service._session = Mock()
            service._session.stop.side_effect = RuntimeError("capture stop failed")
            running = RunningService(service)
            running.thread.start()
            self.assertTrue(service.listening.wait(5))
            service.request_shutdown()
            running.thread.join(5)
            self.assertFalse(running.thread.is_alive())
            self.assertEqual([str(error) for error in running.errors], ["capture stop failed"])
            self.assertEqual(service._discovery.channel.fileno(), -1)
            self.assertFalse(service._discovery.thread.is_alive())
            factory.return_value.stop.assert_called_once()

    def test_failed_http_bind_releases_activated_link(self):
        config = configuration()
        config.network_mode = "auto"
        nodes = config.nodes
        nodes[0].interface = "eth0"
        config.nodes = nodes
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            with patch("bcamsync.http_management._NetworkLink") as factory:
                factory.return_value.start.return_value = "127.0.0.1"
                service = HttpNodeService(config, "node-a", http_port=occupied.getsockname()[1],
                                          discovery_port=udp_port())
                with self.assertRaises(OSError):
                    service.serve()
                factory.return_value.stop.assert_called_once()
                self.assertFalse(service.listening.is_set())
                self.assertIsNone(service.session)


if __name__ == "__main__":
    unittest.main()
