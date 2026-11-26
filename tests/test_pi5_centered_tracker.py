"""Regression checks for the experimental centered tracker on a simulated delayed camera."""

from collections import deque
from pathlib import Path
import subprocess
import sys
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

    def test_centered_mode_rejects_missing_or_zero_measured_lag_before_capture(self):
        program = Path(__file__).resolve().parents[1] / "tools/experiments/pi5_phase_tracking.py"
        args = ["--mode", "centered", "--fps", "24", "--seconds", "10",
                "--calibration", "unused.csv", "--output", "unused-output.csv"]
        for option in ([], ["--lag-frames", "0"]):
            result = subprocess.run([sys.executable, str(program), *args, *option],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("positive measured --lag-frames", result.stderr)


if __name__ == "__main__":
    unittest.main()
