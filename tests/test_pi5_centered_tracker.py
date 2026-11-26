"""Regression checks for the experimental centered tracker on a simulated delayed camera."""

from collections import deque
from types import SimpleNamespace
import unittest

from tools.experiments.pi5_centered_tracker import CenteredTracker


class CenteredTrackerTests(unittest.TestCase):
    def test_one_direction_in_steady_state_and_recovery_from_drift_reversal(self):
        fps, period, lag = 40, 25000, 10
        reference = SimpleNamespace(latest=0)
        follower = SimpleNamespace(latest=0, baseline_us=period)
        tracker = CenteredTracker((reference, follower), (period, 0.15, (-16, -24), (16, 24)), fps, lag)
        queue = deque([0] * lag)
        phase = 150.0
        issued = []
        for i in range(90 * fps):
            drift = 0.15 if i < 45 * fps else -0.15
            phase += drift + queue.popleft()
            reference.latest = i * period * 1000
            follower.latest = reference.latest + round(phase * 1000)
            delta = tracker.request() - period
            queue.append({0: 0, -16: -24, 16: 24}[delta])
            if delta:
                issued.append((i, delta))
            if 15 * fps <= i < 45 * fps or i >= 60 * fps:
                self.assertLess(abs(phase), 24 / 2 + abs(drift))
            elif 45 * fps <= i < 60 * fps:
                # A pending pulse can land just as the free-running rate reverses.
                self.assertLess(abs(phase), 24 + lag * abs(drift))
        self.assertEqual({d for i, d in issued if 15 * fps <= i < 45 * fps}, {-16})
        self.assertEqual({d for i, d in issued if 60 * fps <= i}, {16})
        for (before, _), (after, _) in zip(issued, issued[1:]):
            self.assertGreater(after - before, lag)


if __name__ == "__main__":
    unittest.main()
