"""The node application owns capture and retains networking through cleanup."""

from pathlib import Path
import signal
import socket
import unittest
from unittest.mock import Mock, patch

from bcamsync import Config, NetworkSession
from bcamsync import node as serve_node
from bcamsync.http_management import HttpNodeService

ROOT = Path(__file__).resolve().parents[1]


class NodeApplicationTests(unittest.TestCase):
    def setUp(self):
        self.config = Config.from_file(ROOT / "tests/fixtures/configs/fake_one_node.json")
        self.config.units = [self.config.units[0]]
        self.events = []
        events = self.events

        class Network:
            def __init__(self, config, node):
                self.native = NetworkSession(config, node)

            @property
            def address(self):
                return self.native.address

            @property
            def fault(self):
                return self.native.fault

            def start(self, timeout):
                self.native.start(timeout)

            def resolve(self, timeout):
                return self.native.resolve(timeout)

            def request_stop(self):
                events.append("cancel network waits")
                self.native.request_stop()

            def stop(self):
                events.append("release network")
                self.native.stop()

        class Controller(serve_node.NodeService):
            def request_shutdown(self):
                events.append("close command intake")
                super().request_shutdown()

            def close(self):
                # Discovery cancellation must leave the link alive while
                # capture and synchronization perform their final cleanup.
                if self.network.address != "127.0.0.1":
                    raise AssertionError("network released before capture")
                super().close()
                if self.session is not None and self.session.read("board-a1", 0) is not None:
                    raise AssertionError("capture still delivers after cleanup")
                events.append("close capture")

        self.network_patch = patch.object(serve_node, "NetworkSession", Network)
        self.controller_patch = patch.object(serve_node, "NodeService", Controller)
        self.network_patch.start()
        self.controller_patch.start()
        self.addCleanup(self.network_patch.stop)
        self.addCleanup(self.controller_patch.stop)

    def test_signal_stops_consumers_before_capture_and_network_release(self):
        handlers = {}
        events = self.events

        def install(sig, handler):
            previous = handlers.get(sig, signal.SIG_DFL)
            handlers[sig] = handler
            return previous

        class Server:
            def __init__(self, controller, address, **_):
                self.controller = controller
                self.address = address

            def serve(self):
                self.controller.request("start")
                if not self.controller.session.wait_ready(5):
                    raise AssertionError("capture failed to initialize")
                if self.controller.session.read("board-a1", 1) is None:
                    raise AssertionError("capture failed to start")
                handlers[signal.SIGTERM](signal.SIGTERM, None)
                # Canceling discovery does not terminate the independent link.
                if self.controller.network.address != self.address:
                    raise AssertionError("signal released network prematurely")

            def request_shutdown(self):
                events.append("stop consumers")

            def close(self):
                events.append("join consumers")

        with patch.object(serve_node.signal, "signal", install):
            serve_node.run_node(self.config, "node-a", network_startup="automatic", server_class=Server)
        self.assertEqual(self.events, ["stop consumers", "close command intake", "cancel network waits",
                                       "join consumers", "close command intake", "close capture", "release network"])
        self.assertEqual(handlers[signal.SIGTERM], signal.SIG_DFL)
        self.assertEqual(handlers[signal.SIGINT], signal.SIG_DFL)

    def test_http_bind_failure_releases_application_resources(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as discovery:
            discovery.bind(("127.0.0.1", 0))
            discovery_port = discovery.getsockname()[1]
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            with self.assertRaises(OSError):
                serve_node.run_node(self.config, "node-a", network_startup="automatic", server_class=HttpNodeService,
                                    http_port=occupied.getsockname()[1], discovery_port=discovery_port)
        self.assertEqual(self.events[-2:], ["close capture", "release network"])

    def test_invalid_restart_identity_never_attempts_peer_resolution(self):
        network = NetworkSession(self.config.sync_config(), "node-a")
        network.start()
        self.addCleanup(network.stop)
        controller = serve_node.NodeService(self.config, "node-a", network=network)
        self.addCleanup(controller.close)
        controller.request("start")
        controller.request("stop")
        with patch.object(controller, "network", Mock(wraps=network)) as observed:
            with self.assertRaises(ValueError):
                controller.request("start", "invalid-operation")
            observed.resolve.assert_not_called()
            self.assertFalse(controller.request("status")["active"])
            controller.request("start")
            observed.resolve.assert_called_once()


if __name__ == "__main__":
    unittest.main()
