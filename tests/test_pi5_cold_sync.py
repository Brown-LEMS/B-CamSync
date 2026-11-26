"""The device experiment must observe one public session through acquisition and tracking."""

from contextlib import redirect_stdout
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.experiments import pi5_cold_sync


class ColdSyncTrialTests(unittest.TestCase):
    def test_one_session_covers_lock_tracking_and_restore(self):
        now = [0.0]
        probe = SimpleNamespace(stage="startup")
        transitions = []
        session = Mock()
        session.start.side_effect = lambda: transitions.append(probe.stage)
        session.wait_ready.side_effect = [False, True]
        session.status.side_effect = lambda: SimpleNamespace(
            units=[], ready=probe.stage == "tracking", state=probe.stage, fault="")
        session.stop.side_effect = lambda: transitions.append(probe.stage)

        def sleep(seconds):
            transitions.append(probe.stage)
            now[0] += seconds

        with patch.object(pi5_cold_sync.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(pi5_cold_sync.time, "sleep", side_effect=sleep), redirect_stdout(io.StringIO()):
            pi5_cold_sync.measure(session, [probe], lambda: None, 1, 0)
        self.assertEqual(transitions, ["acquisition"] + ["tracking"] * 4 + ["restoring"])
        self.assertEqual(session.start.call_count, 1)
        self.assertEqual(session.stop.call_count, 1)

    def test_readiness_loss_fails_the_trial_and_restores_controls(self):
        session = Mock()
        session.wait_ready.return_value = True
        session.status.return_value = SimpleNamespace(units=[], ready=False, state="fault", fault="timing")
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "timing"):
            pi5_cold_sync.measure(session, [], lambda: None, 303, 0)
        session.stop.assert_called_once()
