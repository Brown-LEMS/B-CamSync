"""Software checks for operator-tool shutdown ordering and fault reporting."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("validate_device", ROOT / "tools" / "validate_device.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


def state(name="tracking", fault=""):
    return SimpleNamespace(state=name, ready=name == "tracking", fault=fault)


class DeviceValidationShutdownTests(unittest.TestCase):
    def test_follower_recognizes_coordinator_requested_stop(self):
        session = Mock()
        session.status.return_value = state("stopped")
        self.assertTrue(validator.graceful_peer_stop(session, coordinator=False))
        with self.assertRaisesRegex(RuntimeError, "Coordinator stopped"):
            validator.graceful_peer_stop(session, coordinator=True)

    def test_stopped_state_with_a_fault_is_not_graceful(self):
        session = Mock()
        session.status.return_value = state("stopped", "camera shutdown failed")
        with self.assertRaisesRegex(RuntimeError, "camera shutdown failed"):
            validator.graceful_peer_stop(session, coordinator=False)

    def test_follower_waits_without_stopping_local_session(self):
        session = Mock()
        session.status.side_effect = [state(), state("stopped")]
        with patch.object(validator.time, "sleep"), patch.object(validator, "progress"):
            validator.wait_coordinator_stop(session, {}, timeout=1, run=1)
        session.stop.assert_not_called()

    def test_follower_shutdown_wait_has_a_deadline(self):
        session = Mock()
        session.status.return_value = state()
        with patch.object(validator.time, "monotonic", side_effect=[0, 2]), patch.object(validator, "progress"):
            with self.assertRaisesRegex(RuntimeError, "shutdown wait deadline"):
                validator.wait_coordinator_stop(session, {}, timeout=1, run=1)
        session.stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
