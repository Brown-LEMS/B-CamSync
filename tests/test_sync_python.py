"""Python adapters execute real corrections while applications retain capture ownership."""

import gc
import json
import tempfile
from pathlib import Path
import subprocess
import sys
import time
import unittest
import weakref

from bcamsync import Config, PhasePolicy, SyncConfig, SyncSession, SyncUnit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
sys.path.insert(0, str(ROOT / "host"))
from external_capture import SyntheticCapture, local_config


def await_condition(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Adapter condition did not occur before its deadline")
        time.sleep(0.01)


class ObservedCapture(SyntheticCapture):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bindings = []
        self.snapshot_error = False
        self.bad_ack = False
        self.commands = []

    def bind(self, session_id, round_id):
        super().bind(session_id, round_id)
        self.bindings.append((session_id, round_id))

    def snapshot(self):
        if self.snapshot_error:
            raise ValueError("Python snapshot callback failed")
        return super().snapshot()

    def adjust(self, pulse):
        self.commands.append((pulse, (pulse.correction_id, pulse.delta_us, pulse.count, pulse.expires_ns)))
        result = super().adjust(pulse)
        if self.bad_ack:
            result.correction_id += 1
        return result


class DelegatingUnit(SyncUnit):
    """A callback object whose Python lifetime is held only by SyncSession."""

    def __init__(self, capture):
        super().__init__()
        self.capture = capture

    def bind(self, session_id, round_id):
        self.capture.bind(session_id, round_id)

    def snapshot(self):
        return self.capture.snapshot()

    def adjust(self, pulse):
        return self.capture.adjust(pulse)

    def cancel(self):
        self.capture.cancel()

    def fault(self):
        return self.capture.fault()


class ExternalSyncTests(unittest.TestCase):
    def setUp(self):
        self.reference = ObservedCapture("reference")
        self.follower = ObservedCapture("follower", phase_ns=2_000_000)
        self.captures = {"reference": self.reference, "follower": self.follower}
        for capture in self.captures.values():
            capture.start_capture()
        self.session = SyncSession(local_config(), "local", self.captures)

    def tearDown(self):
        self.follower.snapshot_error = False
        self.follower.bad_ack = False
        self.session.stop()
        for capture in self.captures.values():
            capture.stop_capture()

    def test_correction_readiness_stop_and_restart_preserve_capture(self):
        self.session.start()
        with self.assertRaisesRegex(RuntimeError, "stop the session"):
            self.session.set_node_addresses({"local": "127.0.0.1"})
        self.assertTrue(self.session.wait_ready(25))
        completed = self.follower.actuator.snapshot()
        self.assertGreater(completed.correction_id, 0)
        self.assertEqual(completed.state, "restored")
        self.assertGreater(completed.restored_timestamp_ns, 0)
        for pulse, expected in self.follower.commands:
            self.assertEqual((pulse.correction_id, pulse.delta_us, pulse.count, pulse.expires_ns), expected)
        mapping = self.session.clock()
        timing = self.follower.snapshot().frames[-1]
        self.assertTrue(mapping.valid)
        self.assertEqual(mapping.map(timing.sensor_timestamp_ns), timing.sensor_timestamp_ns)
        before = self.follower.sequence
        self.session.stop()
        for pulse, expected in self.follower.commands:
            self.assertEqual((pulse.correction_id, pulse.delta_us, pulse.count, pulse.expires_ns), expected)
        self.assertEqual(self.session.status().state, "stopped")
        await_condition(lambda: self.follower.sequence > before + 2, timeout=1)
        first_binding = self.follower.bindings[-1]
        with self.assertRaises(ValueError):
            self.session.set_node_addresses({})
        self.session.set_node_addresses({"local": "127.0.0.1"})
        self.session.start()
        self.assertTrue(self.session.wait_ready(10))
        self.assertEqual(len(self.follower.bindings), 2)
        self.assertEqual(self.follower.bindings[-1][0], first_binding[0])
        self.assertGreater(self.follower.bindings[-1][1], first_binding[1])
        self.assertTrue(self.follower.thread.is_alive())

    def test_policy_bindings_roundtrip_projection_and_host_contract(self):
        from bcamsync_host import _sync_contract

        config = Config.from_file(ROOT / "examples" / "configs" / "fake_two_nodes.json")
        default_document = json.loads(config.to_json())
        default_document.pop("phase_policy")
        self.assertEqual(json.loads(config.sync_json()), _sync_contract(default_document))
        policy = PhasePolicy()
        policy.startup_delta_us = 100
        policy.tracking_delta_us = 50
        policy.window_seconds = 1.5
        policy.max_mapping_span_ns = 50_000
        config.phase_policy = policy
        self.assertEqual(config.clock_mode, "host")
        expected = json.loads(config.sync_json())
        self.assertEqual(expected, _sync_contract(json.loads(config.to_json())))
        self.assertEqual(expected, json.loads(config.sync_config().sync_json()))
        self.assertEqual(expected, _sync_contract(json.loads(config.sync_config().to_json())))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(config.to_json())
            restored = Config.from_file(path)
            self.assertEqual(restored.to_json(), config.to_json())
            path.write_text(config.sync_config().to_json())
            self.assertEqual(SyncConfig.from_file(path).to_json(), config.sync_config().to_json())
        config.backend = "arducam"
        self.assertEqual(config.clock_mode, "host")
        self.assertEqual(json.loads(config.sync_config().sync_json()), expected)
        policy.startup_delta_us = 1001
        config.phase_policy = policy
        with self.assertRaises(ValueError):
            config.validate()
        invalid = local_config()
        invalid.phase_policy.startup_delta_us = 1001
        with self.assertRaises(ValueError):
            SyncSession(invalid, "local", self.captures)
        self.assertEqual(self.follower.bindings, [])

    def test_python_adapter_callbacks_survive_dropped_references(self):
        self.session.stop()
        adapters = {unit_id: DelegatingUnit(capture) for unit_id, capture in self.captures.items()}
        references = [weakref.ref(adapter) for adapter in adapters.values()]
        self.session = SyncSession(local_config(), "local", adapters)
        del adapters
        gc.collect()
        self.assertTrue(all(reference() is not None for reference in references))
        self.session.start()
        self.assertTrue(self.session.wait_ready(25))
        self.session.stop()
        del self.session
        gc.collect()
        self.session = SyncSession(local_config(), "local", self.captures)
        self.assertTrue(all(reference() is None for reference in references))

    def test_new_sync_session_attaches_to_ongoing_capture(self):
        self.session.start()
        await_condition(lambda: self.follower.binding is not None)
        prior_binding = self.follower.binding
        producer = self.follower.thread
        self.session.stop()
        before = self.follower.sequence
        await_condition(lambda: self.follower.sequence > before + 2, timeout=1)
        self.session = SyncSession(local_config(), "local", self.captures)
        self.session.start()
        self.assertTrue(self.session.wait_ready(25))
        self.assertNotEqual(self.follower.binding[0], prior_binding[0])
        self.assertIs(self.follower.thread, producer)
        self.assertTrue(producer.is_alive())
        self.assertGreater(self.follower.sequence, before)
        self.assertGreater(self.follower.actuator.snapshot().correction_id, 0)

    def test_python_callback_exception_reaches_status_without_stopping_capture(self):
        self.follower.snapshot_error = True
        self.session.start()
        with self.assertRaisesRegex(RuntimeError, "Python snapshot callback failed"):
            self.session.wait_ready(10)
        self.assertIn("Python snapshot callback failed", self.session.status().fault)
        self.assertFalse(self.session.status().ready)
        before = self.follower.sequence
        await_condition(lambda: self.follower.sequence > before + 2, timeout=1)

    def test_wrong_correction_acknowledgement_is_not_accepted(self):
        self.follower.bad_ack = True
        self.session.start()
        with self.assertRaisesRegex(RuntimeError, "acknowledgement"):
            self.session.wait_ready(10)
        self.assertTrue(self.reference.thread.is_alive())
        self.assertTrue(self.follower.thread.is_alive())

    def test_destructor_releases_gil_for_active_restoration_callbacks(self):
        script = '''
import gc
import sys
import time
sys.path.insert(0, sys.argv[1])
from external_capture import SyntheticCapture, local_config
from bcamsync import SyncSession
captures = {"reference": SyntheticCapture("reference"),
            "follower": SyntheticCapture("follower", phase_ns=4_000_000)}
for capture in captures.values():
    capture.start_capture()
try:
    session = SyncSession(local_config(), "local", captures)
    session.start()
    deadline = time.monotonic() + 8
    while captures["follower"].actuator.snapshot().state != "submitting":
        assert time.monotonic() < deadline, "No correction before destructor test"
        time.sleep(.005)
    del session
    gc.collect()
    assert captures["follower"].actuator.snapshot().state == "restored"
    assert captures["follower"].thread.is_alive()
finally:
    for capture in captures.values():
        capture.stop_capture()
'''
        completed = subprocess.run([sys.executable, "-c", script, str(ROOT / "examples")],
                                   capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
