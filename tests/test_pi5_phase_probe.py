"""No camera needed for the experimental single-frame quantized request law."""

import csv
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "experiments"))
from pi5_phase_tracking import Tracker, identify, limit_phase_policy


class PhaseProbeTests(unittest.TestCase):
    def test_initial_lock_respects_mode_headroom_for_stock_and_adaptive(self):
        policy = SimpleNamespace(startup_delta_us=1000, tracking_delta_us=200)
        cfg = SimpleNamespace(phase_policy=policy, validate=lambda: None)
        camera = SimpleNamespace(baseline_us=22727, minimum_us=22221, maximum_us=199993)
        limit_phase_policy(cfg, camera)
        self.assertEqual(cfg.phase_policy.startup_delta_us, 506)
        self.assertEqual(cfg.phase_policy.tracking_delta_us, 200)

    def test_request_cancels_either_drift_sign_with_quantized_effect_and_latency(self):
        for fps, bias in [(24, -0.18), (40, 0.12)]:
            period = 1_000_000 // fps
            reference = SimpleNamespace(latest=0)
            follower = SimpleNamespace(baseline_us=period, latest=0)
            tracker = Tracker((reference, follower), (period, bias, (-8, -24), (16, 24)), fps)
            phase = 140
            pending = [0] * 9
            residuals = []
            for i in range(60 * fps):
                reference.latest = i * period * 1000
                follower.latest = reference.latest + round(phase * 1000)
                requested = tracker.request() - period
                pending.append({-8: -24, 0: 0, 16: 24}[requested])
                phase += bias + pending.pop(0)
                if i >= 10 * fps:
                    residuals.append(abs(phase))
            self.assertLess(max(residuals), 40)

    def test_unachievable_phase_request_does_not_wind_up_accumulated_debt(self):
        ref = SimpleNamespace(latest=0)
        follower = SimpleNamespace(baseline_us=25000, latest=10_000_000)
        tracker = Tracker((ref, follower), (25000, 0, (-16, -24), (12, 24)), 40)
        for _ in range(30):
            self.assertEqual(tracker.request(), 24984)
        follower.latest = 0
        self.assertEqual(tracker.request(), 25000)

    def test_identification_ignores_ineffective_integer_commands(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sweep.csv"
            with path.open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(("unit", "sequence", "sensor_ns", "stage", "requested_us"))
                steps = [("baseline", 0)]
                for repeat in (1, 2):
                    steps += [(f"step{repeat}_{d:+d}", d) for d in [-12, -1, 1, 16]]
                for unit in ("board-1", "board-2"):
                    timestamp = sequence = 0
                    for stage, delta in steps:
                        applied = delta if unit == "board-2" else 0
                        q = {-12: -24, -1: 0, 0: 0, 1: 0, 16: 24}[applied]
                        for _ in range(25):
                            timestamp += 25_000_000 + q * 1000 + (200 if unit == "board-2" else 0)
                            writer.writerow((unit, sequence, timestamp, stage, 25000 + applied))
                            sequence += 1
            ref, drift, negative, positive = identify(path, 25000)
            self.assertEqual(ref, 25000)
            self.assertAlmostEqual(drift, 0.2)
            self.assertEqual(negative, (-12, -24))
            self.assertEqual(positive, (16, 24))


if __name__ == "__main__":
    unittest.main()
