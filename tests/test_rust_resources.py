"""SDK-free session ownership and Linux process resource regression checks."""

import gc
import os
import sys
import time
import unittest
import weakref

from bcamsync import (
    FrameTiming, NodeConfig, SyncConfig, SyncSession, SyncUnit,
    SyncUnitConfig, TimingSnapshot, boot_time_ns,
)


def local_config():
    node = NodeConfig()
    node.id = "local"
    unit = SyncUnitConfig()
    unit.id = "reference"
    unit.node_id = node.id
    config = SyncConfig()
    config.group_id = "resource-regression"
    config.reference_unit = unit.id
    config.network_mode = "existing"
    config.nodes = [node]
    config.units = [unit]
    config.validate()
    return config


class ReferenceTiming(SyncUnit):
    """Application-owned timing source with no image buffers or producer thread."""

    def __init__(self, observations):
        super().__init__()
        self.observations = observations
        self.sequence = 0

    def bind(self, session_id, round_id):
        self.observations["bindings"] += 1

    def snapshot(self):
        self.sequence += 1
        now = boot_time_ns()
        frame = FrameTiming()
        frame.camera_sequence = self.sequence
        frame.sensor_timestamp_ns = now
        frame.timestamp_ns = now
        frame.requested_duration_us = 25_000
        frame.reported_duration_us = 25_000
        result = TimingSnapshot()
        result.unit_id = "reference"
        result.snapshot_timestamp_ns = now
        result.frames = [frame]
        self.observations["snapshots"] += 1
        return result

    def adjust(self, pulse):
        raise AssertionError("The reference unit must not receive phase corrections")

    def cancel(self):
        self.observations["cancellations"] += 1

    def fault(self):
        return ""


def resource_counts():
    return {
        "file descriptors": len(os.listdir("/proc/self/fd")),
        "native threads": len(os.listdir("/proc/self/task")),
    }


class RustResourceTests(unittest.TestCase):
    def run_session(self):
        observations = {"bindings": 0, "snapshots": 0, "cancellations": 0}
        adapter = ReferenceTiming(observations)
        reference = weakref.ref(adapter)
        session = SyncSession(local_config(), "local", {"reference": adapter})
        del adapter
        gc.collect()
        self.assertIsNotNone(reference(), "Session must retain its Python adapter")
        try:
            session.start()
            self.assertTrue(session.wait_ready(2), "Reference timing did not become ready")
            self.assertEqual(observations["bindings"], 1)
            self.assertGreater(observations["snapshots"], 0,
                               "Python callbacks must run after external references are dropped")
            self.assertTrue(session.status().ready)
        finally:
            session.stop()
        self.assertEqual(session.status().state, "stopped")
        self.assertFalse(session.clock().valid)
        self.assertIsNotNone(reference(), "A stopped session still owns its adapter for restart")
        self.assertGreater(observations["cancellations"], 0)
        del session
        gc.collect()
        self.assertIsNone(reference(), "Destroying the session must release its Python adapter")

    def test_python_callbacks_survive_external_reference_release(self):
        self.run_session()

    @unittest.skipUnless(sys.platform == "linux", "Resource counts require Linux /proc")
    def test_repeated_sessions_release_native_threads_and_file_descriptors(self):
        for _ in range(5):
            self.run_session()
        baseline = resource_counts()
        for completed in range(1, 101):
            self.run_session()
            if completed % 25 == 0:
                self.assert_resources_return(baseline)
                print(f"Session resource check: {completed}/100 cycles completed", flush=True)
        self.assert_resources_return(baseline)

    def assert_resources_return(self, baseline):
        deadline = time.monotonic() + 3
        current = resource_counts()
        while current != baseline and time.monotonic() < deadline:
            time.sleep(0.01)
            current = resource_counts()
        self.assertEqual(current, baseline, "Native resources grew after session destruction")


if __name__ == "__main__":
    unittest.main()
