"""Reusable device adapters work without pixels, SDKs, or managed acquisition."""

from contextlib import redirect_stdout
from io import StringIO
import threading
import time
import unittest

import bcamsync
from bcamsync import NodeConfig, PhasePolicy, SyncConfig, SyncSession, SyncUnitConfig
from bcamsync import adapters
from bcamsync.adapters import Pulse, boot_time_ns
from bcamsync.adapters.libcamera import CameraTiming


class CameraTimingTests(unittest.TestCase):
    def setUp(self):
        self.unit = CameraTiming("board", 40, 20000, 30000)
        self.unit.bind("adapter-test", 1)

    def pulse(self, *, expires_ns=None):
        pulse = Pulse()
        pulse.correction_id, pulse.delta_us, pulse.count = 1, 200, 2
        pulse.expires_ns = expires_ns if expires_ns is not None else boot_time_ns() + 1_000_000_000
        return pulse

    def test_legacy_exports_are_the_same_adapter_types(self):
        for name in adapters.__all__:
            self.assertIs(getattr(adapters, name), getattr(bcamsync, name), name)

    def test_capture_can_submit_and_observe_before_a_sync_session(self):
        unit = CameraTiming("standalone", 40, 20000, 30000)
        submitted = unit.next_submission()
        self.assertEqual(submitted.duration_us, 25000)
        sensor_ns = boot_time_ns()
        unit.observe(1, sensor_ns, 25000, submitted)
        self.assertEqual(unit.snapshot().frames[0].sensor_timestamp_ns, sensor_ns)
        self.assertEqual(unit.snapshot().correction.state, "idle")

    def test_metadata_confirms_restoration_at_the_libcamera_tolerance(self):
        output = StringIO()
        with redirect_stdout(output):
            self.unit.adjust(self.pulse())
        self.assertEqual(output.getvalue(), "", "The adapter leaves application logging to its caller")
        self.assertEqual(self.unit.corrections, 1)
        submitted = [self.unit.next_submission() for _ in range(3)]
        self.assertEqual([item.duration_us for item in submitted], [25200, 25200, 25000])
        now = boot_time_ns()
        self.unit.observe(1, now, 25051, submitted[-1])
        self.assertEqual(self.unit.snapshot().correction.state, "settling")
        self.unit.observe(2, now + 25050000, 25050, self.unit.next_submission())
        snapshot = self.unit.snapshot()
        self.assertEqual(snapshot.correction.state, "restored")
        self.assertEqual(snapshot.correction.restored_timestamp_ns, now + 25050000)
        self.assertEqual(snapshot.frames[-1].sensor_timestamp_ns, now + 25050000)
        self.assertEqual(snapshot.frames[-1].timestamp_ns, now + 25050000)

    def test_cancel_and_rebind_keep_capture_observations(self):
        self.unit.adjust(self.pulse())
        self.unit.next_submission()
        self.unit.cancel()
        restore = self.unit.next_submission()
        self.assertTrue(restore.restoration)
        self.unit.observe(1, boot_time_ns(), 25000, restore)
        self.assertEqual(self.unit.snapshot().correction.state, "restored")
        self.unit.bind("adapter-test", 2)
        snapshot = self.unit.snapshot()
        self.assertEqual(snapshot.correction.state, "idle")
        self.assertEqual(snapshot.correction.correction_id, 0)
        self.assertEqual(snapshot.frames[-1].camera_sequence, 1)
        self.assertEqual(self.unit.next_submission().round_id, 2)

    def test_expired_or_out_of_range_controls_do_not_change_the_baseline(self):
        expired = self.pulse(expires_ns=boot_time_ns() - 1)
        out_of_range = self.pulse()
        out_of_range.delta_us = 1001
        for pulse in (expired, out_of_range):
            with self.assertRaises(RuntimeError):
                self.unit.adjust(pulse)
        self.assertEqual(self.unit.corrections, 0)
        self.assertEqual(self.unit.next_submission().duration_us, 25000)

    def test_timing_history_is_bounded_and_snapshots_are_independent(self):
        now = boot_time_ns()
        for sequence in range(300):
            self.unit.observe(sequence, now + sequence * 25000000, 25000,
                              self.unit.next_submission())
        snapshot = self.unit.snapshot()
        self.assertEqual(len(snapshot.frames), 240)
        self.assertEqual(snapshot.frames[0].camera_sequence, 60)
        frames = snapshot.frames
        frames[-1].sensor_timestamp_ns = 1
        self.assertEqual(self.unit.snapshot().frames[-1].sensor_timestamp_ns,
                         now + 299 * 25000000)
        self.unit.error = "camera disconnected"
        self.assertEqual(self.unit.fault(), "camera disconnected")

    def test_session_chooses_corrections_and_stop_leaves_capture_running(self):
        node = NodeConfig()
        node.id = "local"
        config = SyncConfig()
        config.group_id, config.reference_unit = "adapter-integration", "reference"
        config.nodes = [node]
        members = []
        for name in ("reference", "follower"):
            member = SyncUnitConfig()
            member.id, member.node_id = name, node.id
            members.append(member)
        config.units = members
        policy = PhasePolicy()
        policy.window_seconds, policy.evaluation_seconds = 0.15, 0.05
        policy.settling_seconds, policy.application_delay_seconds = 0.025, 0.025
        policy.correction_interval_seconds, policy.startup_frames = 0.1, 4
        config.phase_policy, config.startup_timeout_seconds = policy, 8
        units = {name: CameraTiming(name, 40, 20000, 30000)
                 for name in ("reference", "follower")}
        done = threading.Event()
        errors = []

        def capture():
            due = {"reference": boot_time_ns() + 10000000}
            due["follower"] = due["reference"] + 2000000
            sequences = dict.fromkeys(units, 0)
            try:
                while not done.is_set():
                    name = min(due, key=due.get)
                    if done.wait(max(0, due[name] - boot_time_ns()) / 1e9):
                        break
                    unit = units[name]
                    submitted = unit.next_submission()
                    sequences[name] += 1
                    unit.observe(sequences[name], due[name], submitted.duration_us, submitted)
                    due[name] += submitted.duration_us * 1000
            except Exception as error:
                errors.append(error)
                for unit in units.values():
                    unit.error = str(error)

        worker = threading.Thread(target=capture, name="adapter-test-capture")
        session = SyncSession(config, node.id, units)
        worker.start()
        try:
            session.start()
            self.assertTrue(session.wait_ready(8), "Library-generated corrections did not settle")
            self.assertGreater(units["follower"].corrections, 0)
            self.assertEqual(units["reference"].corrections, 0)
            session.stop()
            self.assertEqual(session.status().state, "stopped")
            self.assertEqual(units["follower"].snapshot().correction.state, "restored")
            sequence = units["reference"].snapshot().frames[-1].camera_sequence
            time.sleep(0.08)
            self.assertGreater(units["reference"].snapshot().frames[-1].camera_sequence, sequence)
        finally:
            try:
                session.stop()
            finally:
                done.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
