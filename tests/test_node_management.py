"""Automatic networking stays independent of capture and explicit startup."""

from io import StringIO
from pathlib import Path
import signal
import unittest
from unittest.mock import Mock, patch

from bcamsync import Config, NetworkSession, SyncConfig
from bcamsync import network as network_app, node as node_app
from bcamsync.management import NodeService


ROOT = Path(__file__).resolve().parents[1]


class NodeStartupTests(unittest.TestCase):
    def setUp(self):
        self.config = Config.from_file(ROOT / "tests/fixtures/configs/fake_one_node.json")
        self.config.units = [self.config.units[0]]
        self.handlers = {}

        def install(sig, handler):
            previous = self.handlers.get(sig, signal.SIG_DFL)
            self.handlers[sig] = handler
            return previous

        self.signals = patch.object(node_app.signal, "signal", install)
        self.signals.start()
        self.addCleanup(self.signals.stop)

    def network(self):
        native = NetworkSession(self.config.sync_config(), "node-a")
        self.addCleanup(native.stop)
        observed = Mock(wraps=native, address="127.0.0.1", fault="")
        return native, observed

    def test_automatic_network_is_available_without_resolving_peers_or_creating_cameras(self):
        self.config.backend = "arducam"
        native, observed = self.network()
        observed.resolve.side_effect = AssertionError("idle networking must not wait for peers")
        test = self

        class Server:
            def __init__(self, controller, address, **_):
                self.controller = controller
                test.assertEqual(address, "127.0.0.1")

            def serve(self):
                test.assertEqual(native.address, "127.0.0.1")
                test.assertIsNone(self.controller.session)
                status = self.controller.request("status")
                test.assertFalse(status["active"])
                test.assertEqual(status["network"], {"address": "127.0.0.1", "fault": ""})

            def close(self):
                pass

        with patch.object(node_app, "NetworkSession", return_value=observed), \
                patch("bcamsync_managed.Session", side_effect=AssertionError("camera initialized")):
            node_app.run_node(self.config, "node-a", network_startup="automatic", server_class=Server)
        self.assertEqual(native.address, "")
        observed.start.assert_called_once()
        observed.stop.assert_called_once()

    def test_explicit_default_does_not_start_networking_or_cameras(self):
        test = self

        class Server:
            def __init__(self, controller, address, **_):
                test.assertIsNone(controller.network)
                test.assertIsNone(controller.session)
                test.assertEqual(address, "127.0.0.1")

            def serve(self):
                pass

            def close(self):
                pass

        with patch.object(node_app, "NetworkSession", side_effect=AssertionError("network initialized")), \
                patch("bcamsync_managed.Session", side_effect=AssertionError("camera initialized")):
            node_app.run_node(self.config, "node-a", server_class=Server)

    def test_signal_during_local_network_startup_releases_network_without_opening_http(self):
        network = Mock()

        def start(_):
            self.handlers[signal.SIGTERM]()
            raise RuntimeError("node platform startup cancelled")

        network.start.side_effect = start
        with patch.object(node_app, "NetworkSession", return_value=network):
            server = Mock(side_effect=AssertionError("HTTP opened during cancellation"))
            node_app.run_node(self.config, "node-a", network_startup="automatic", server_class=server)
        network.request_stop.assert_called_once()
        network.stop.assert_called_once()
        self.assertEqual(self.handlers[signal.SIGTERM], signal.SIG_DFL)

    def test_network_failure_is_reported_and_cleaned_up(self):
        network = Mock()
        network.start.side_effect = RuntimeError("network activation failed")
        with patch.object(node_app, "NetworkSession", return_value=network):
            with self.assertRaisesRegex(RuntimeError, "activation failed"):
                node_app.run_node(self.config, "node-a", network_startup="automatic")
        network.stop.assert_called_once()

    def test_peer_timeout_preserves_network_and_delays_camera_initialization(self):
        native, observed = self.network()
        native.start(1)
        observed.resolve.side_effect = RuntimeError("network session deadline exceeded")
        controller = NodeService(self.config, "node-a", network=observed)
        self.addCleanup(controller.close)
        with patch("bcamsync_managed.Session", side_effect=AssertionError("camera initialized")):
            with self.assertRaisesRegex(RuntimeError, "deadline exceeded"):
                controller.request("start")
        self.assertIsNone(controller.session)
        self.assertEqual(native.address, "127.0.0.1")
        self.assertFalse(controller.request("status")["active"])
        observed.resolve.side_effect = None
        controller.request("start")
        session = controller.session
        self.assertTrue(session.wait_ready(2))
        controller.request("stop")
        controller.request("start")
        self.assertIs(controller.session, session)
        self.assertTrue(session.wait_ready(2))
        self.assertEqual(observed.resolve.call_count, 3)
        controller.close()
        observed.stop.assert_not_called()
        self.assertEqual(native.address, "127.0.0.1")

    def test_network_only_service_waits_without_peers_and_stops_on_signal(self):
        native, observed = self.network()
        observed.resolve.side_effect = AssertionError("network service must not require peers")
        waits = Mock()
        calls = []

        def wait(_):
            calls.append(True)
            if len(calls) == 1:
                self.assertEqual(native.address, "127.0.0.1")
                return False
            self.handlers[signal.SIGTERM]()
            return True

        waits.wait.side_effect = wait
        with patch.object(network_app, "NetworkSession", return_value=observed), \
                patch.object(network_app.threading, "Event", return_value=waits), \
                patch("sys.stdout", new=StringIO()), \
                patch("bcamsync_managed.Session", side_effect=AssertionError("camera initialized")):
            network_app.run_network(self.config.sync_config(), "node-a")
        self.assertEqual(native.address, "")
        observed.request_stop.assert_called_once()
        observed.stop.assert_called_once()
        self.assertEqual(self.handlers[signal.SIGINT], signal.SIG_DFL)

    def test_network_only_service_reports_background_discovery_fault(self):
        native, observed = self.network()
        observed.fault = "discovered node synchronization configuration differs"
        with patch.object(network_app, "NetworkSession", return_value=observed), \
                patch("sys.stdout", new=StringIO()):
            with self.assertRaisesRegex(RuntimeError, "configuration differs"):
                network_app.run_network(self.config.sync_config(), "node-a")
        self.assertEqual(native.address, "")

    def test_explicit_startup_requires_an_existing_http_address(self):
        nodes = self.config.nodes
        nodes[0].address = ""
        self.config.nodes = nodes
        with self.assertRaisesRegex(ValueError, "requires --address"):
            node_app.run_node(self.config, "node-a")
        with self.assertRaisesRegex(ValueError, "automatic or explicit"):
            node_app.run_node(self.config, "node-a", network_startup="invalid")

    def test_packaged_cli_defaults_to_explicit_and_accepts_automatic_opt_in(self):
        path = str(ROOT / "tests/fixtures/configs/fake_one_node.json")
        with patch.object(node_app, "run_node") as run:
            self.assertEqual(node_app.main([path, "node-a"]), 0)
            self.assertEqual(run.call_args.kwargs["network_startup"], "explicit")
            self.assertEqual(node_app.main([path, "node-a", "--network-startup", "automatic"]), 0)
            self.assertEqual(run.call_args.kwargs["network_startup"], "automatic")
        with patch.object(network_app, "run_network") as run:
            self.assertEqual(network_app.main([path, "node-a"]), 0)
            self.assertIsInstance(run.call_args.args[0], SyncConfig)
            self.assertEqual(run.call_args.args[0].group_id, self.config.group_id)
            self.assertEqual(len(run.call_args.args[0].units), 2)


if __name__ == "__main__":
    unittest.main()
