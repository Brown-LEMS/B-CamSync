"""Public-package fake acquisition, closed-loop synchronization, and ownership.

These checks run in Linux CI against the normal Python package, including a
wheel installation. They do not import the private test-support extension.
"""

import gc
from pathlib import Path
import socket
import time
import unittest

import numpy as np

from bcamsync import Config, Session


ROOT = Path(__file__).resolve().parents[1]


def config_from_example(name: str):
    config = Config.from_file(ROOT / "examples" / "configs" / name)
    # Reserve distinct endpoints together so concurrent CI jobs do not share
    # the fixed example ports. Runtime still uses ordinary localhost TCP.
    reservations = []
    nodes = config.nodes
    try:
        for node in nodes:
            endpoint = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            reservations.append(endpoint)
            endpoint.bind(("127.0.0.1", 0))
            node.port = endpoint.getsockname()[1]
        config.nodes = nodes
    finally:
        for endpoint in reservations:
            endpoint.close()
    config.validate()
    return config


class FakePublicApiTests(unittest.TestCase):
    def assert_packet(self, packet, unit_id):
        self.assertIsNotNone(packet)
        self.assertEqual(packet.unit_id, unit_id)
        self.assertEqual((packet.width, packet.height), (64, 16))
        self.assertEqual(len(packet.views), 4)
        for view in packet.views:
            self.assertEqual(view.shape, (16, 16))
            self.assertEqual(view.strides, (64, 1))
            self.assertEqual(view.dtype, np.uint8)
            self.assertFalse(view.flags.writeable)
        self.assertGreater(packet.sensor_timestamp_ns, 0)
        self.assertGreater(packet.timestamp_ns, 0)
        self.assertTrue(packet.clock_valid)

    def assert_ready_metrics(self, session):
        status = session.status()
        self.assertTrue(status.ready)
        self.assertEqual(status.state, "tracking")
        self.assertEqual(status.fault, "")
        for unit in status.units:
            self.assertTrue(unit.ready)
            if unit.state != "reference":
                self.assertGreater(unit.measurement_end_ns, 0)
                self.assertLessEqual(unit.p95_ms, 0.5)
                self.assertLessEqual(unit.max_ms, 1.0)

    def assert_cleanly_stopped(self, session):
        status = session.status()
        self.assertEqual(status.state, "stopped")
        self.assertFalse(status.ready)
        self.assertEqual(status.fault, "")

    def test_local_phase_correction_frame_retention_and_session_restart(self):
        config = config_from_example("fake_one_node.json")
        self.assertEqual(config.backend, "fake")
        self.assertEqual(config.clock_mode, "host")
        self.assertEqual(config.fps, 40)
        self.assertEqual(config.unit("board-a2").fake.initial_phase_us, 2000)
        self.assertEqual(config.unit("board-a2").fake.drift_ppm, 10)
        session = Session(config, "node-a")
        retained = {}
        try:
            session.start()
            self.assertTrue(session.wait_ready(45))
            self.assert_ready_metrics(session)
            previous = {}
            for unit_id in ("board-a1", "board-a2"):
                packet = session.read(unit_id, 1)
                self.assert_packet(packet, unit_id)
                views = packet.views
                retained[unit_id] = (views, tuple(view.copy() for view in views))
                previous[unit_id] = packet.sequence
            with self.assertRaises(ValueError):
                views[0].setflags(write=True)
            with self.assertRaises(IndexError):
                packet.view(4)
            for _ in range(10):
                for unit_id in previous:
                    packet = session.read(unit_id, 1)
                    self.assert_packet(packet, unit_id)
                    self.assertGreater(packet.sequence, previous[unit_id])
                    previous[unit_id] = packet.sequence
            for views, original in retained.values():
                for view, copy in zip(views, original):
                    np.testing.assert_array_equal(view, copy)
            # Slow image consumption evicts undelivered packets while phase
            # observation and correction remain active in the native runtime.
            time.sleep(0.25)
            fresh = session.read("board-a1", 1)
            self.assert_packet(fresh, "board-a1")
            self.assertGreater(fresh.dropped_before, 0)
            self.assert_ready_metrics(session)
            fragment = retained["board-a2"][0][2][::2, 1:]
            expected = fragment.copy()
            session.stop()
            self.assert_cleanly_stopped(session)
            del retained, views, view, packet, fresh
            gc.collect()
            np.testing.assert_array_equal(fragment, expected)
            session.start()
            self.assertTrue(session.wait_ready(45))
            self.assert_ready_metrics(session)
            replacement = session.read("board-a2", 1)
            self.assert_packet(replacement, "board-a2")
            np.testing.assert_array_equal(fragment, expected)
        finally:
            session.stop()
        self.assert_cleanly_stopped(session)
        del replacement, session
        gc.collect()
        np.testing.assert_array_equal(fragment, expected)

    def test_same_host_two_nodes_share_control_and_coordinator_shutdown(self):
        config = config_from_example("fake_two_nodes.json")
        self.assertEqual(config.nodes[0].address, config.nodes[1].address)
        self.assertNotEqual(config.nodes[0].port, config.nodes[1].port)
        follower = Session(config, "node-b")
        coordinator = Session(config, "node-a")
        try:
            follower.start()
            coordinator.start()
            self.assertTrue(coordinator.wait_ready(45))
            self.assertTrue(follower.wait_ready(5))
            self.assert_ready_metrics(coordinator)
            self.assert_ready_metrics(follower)
            reference_packet = coordinator.read("board-a1", 1)
            follower_packet = follower.read("board-b1", 1)
            self.assert_packet(reference_packet, "board-a1")
            self.assert_packet(follower_packet, "board-b1")
            with self.assertRaises(ValueError):
                coordinator.read("board-b1", 0)
            fragment = follower_packet.view(3)[1::2, :]
            expected = fragment.copy()
            coordinator.stop()
            self.assert_cleanly_stopped(coordinator)
            deadline = time.monotonic() + 10
            while follower.status().state != "stopped":
                self.assertEqual(follower.status().fault, "")
                if time.monotonic() >= deadline:
                    self.fail("Follower did not honor coordinator shutdown")
                time.sleep(0.05)
            self.assert_cleanly_stopped(follower)
            self.assertEqual(follower.status().stop_node, "node-a")
            self.assertEqual(follower.status().stop_reason, "requested")
            previous = follower_packet.sequence
            # Read more than the queue capacity to prove capture is producing
            # new frames after synchronization, rather than draining old packets.
            for _ in range(config.unit("board-b1").queue_capacity + 4):
                packet = follower.read("board-b1", 1)
                self.assertIsNotNone(packet)
                self.assertGreater(packet.sequence, previous)
                self.assertGreater(packet.sensor_timestamp_ns, 0)
                self.assertFalse(packet.clock_valid)
                self.assertEqual(packet.timestamp_ns, 0)
                previous = packet.sequence
            with self.assertRaises(RuntimeError):
                follower.start()
            follower.stop()
            self.assertIsNone(follower.read("board-b1", 0))
            self.assertEqual(follower.status().stop_node, "node-a")
        finally:
            coordinator.stop()
            follower.stop()
        self.assert_cleanly_stopped(follower)
        del follower_packet, reference_packet, coordinator, follower
        gc.collect()
        np.testing.assert_array_equal(fragment, expected)


if __name__ == "__main__":
    unittest.main()
