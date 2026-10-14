"""Network lifetime through the public API, without cameras, HTTP, or clock processes."""

import unittest

from bcamsync import NetworkSession, NodeConfig, SyncConfig, SyncUnitConfig


def configuration():
    config = SyncConfig()
    config.group_id = "network-python"
    config.reference_unit = "board-a"
    config.network_mode = "existing"
    nodes, units = [], []
    for node_id, unit_id, address in [("node-a", "board-a", "127.0.0.1"), ("node-b", "board-b", "127.0.0.2")]:
        node = NodeConfig()
        node.id, node.address = node_id, address
        unit = SyncUnitConfig()
        unit.id, unit.node_id = unit_id, node_id
        nodes.append(node)
        units.append(unit)
    config.nodes, config.units = nodes, units
    return config


class NetworkSessionTests(unittest.TestCase):
    def test_context_establishes_network_without_capture_and_retains_peer_addresses(self):
        config = configuration()
        network = NetworkSession(config, "node-a")
        self.assertEqual(network.address, "")
        with network as active:
            self.assertIs(active, network)
            self.assertEqual(active.address, "127.0.0.1")
            resolved = active.resolve(1)
            self.assertEqual(resolved.to_json(), config.to_json())
            self.assertEqual(resolved.node("node-b").address, "127.0.0.2")
            with self.assertRaisesRegex(RuntimeError, "already started"):
                active.start(1)
        self.assertEqual(network.address, "")
        with self.assertRaisesRegex(RuntimeError, "not started"):
            network.resolve(1)
        network.start(1)
        self.assertEqual(network.resolve(1).sync_json(), config.sync_json())
        network.close()

    def test_invalid_timeouts_do_not_establish_network(self):
        network = NetworkSession(configuration(), "node-a")
        for duration in (0, -1, float("nan"), float("inf"), 1e20):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                network.start(duration)
        self.assertEqual(network.address, "")

    def test_cancellation_retains_address_until_explicit_cleanup(self):
        network = NetworkSession(configuration(), "node-a")
        network.request_stop()
        network.request_stop()
        network.start(1)
        network.request_stop()
        self.assertEqual(network.address, "127.0.0.1")
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            network.resolve(1)
        network.stop()
        self.assertEqual(network.address, "")
        network.start(1)
        self.assertEqual(network.resolve(1).node("node-b").address, "127.0.0.2")
        network.stop()

    def test_invalid_local_identity_and_address_are_rejected(self):
        config = configuration()
        with self.assertRaises(ValueError):
            NetworkSession(config, "missing")
        nodes = config.nodes
        nodes[0].address = "239.1.2.3"
        config.nodes = nodes
        with self.assertRaises(ValueError):
            NetworkSession(config, "node-a")


if __name__ == "__main__":
    unittest.main()
