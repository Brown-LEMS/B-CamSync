import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.experiments.summarize_period_response import summarize
from tools.experiments.summarize_tracking import score


class PeriodResponseSummaryTest(unittest.TestCase):
    def test_detects_shared_timing_gap_despite_complete_sequence_pairing(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tracking.csv"
            with path.open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["unit", "sequence", "sensor_ns", "requested_us", "stage"])
                for seq in range(12):
                    t = (seq + (2 if seq >= 4 else 0)) * 25_000_000
                    for unit in ("board-1", "board-2"):
                        writer.writerow([unit, seq, t, 25000, "stock"])
            result = score(path, "stock", 40, 0, .25)
            self.assertEqual(result["pairs"], result["expected_by_sequence"])
            self.assertEqual(result["max_us"], 0)
            for key in ("reference_cadence", "follower_cadence"):
                self.assertEqual(result[key], {"intervals_over_1_5_periods": 1,
                                               "max_interval_us": 75000})

    def test_fixed_pairing_counts_missing_frames_and_preserves_phase_excursion(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tracking.csv"
            with path.open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["unit", "sequence", "sensor_ns", "requested_us", "stage"])
                for i in range(12):
                    t = i * 25_000_000
                    writer.writerow(["board-1", i + 100, t, 25000, "adaptive"])
                    if i != 6:
                        offset = 15000000 if i == 7 else 10000
                        writer.writerow(["board-2", i + 20, t + offset, 25000, "adaptive"])
            result = score(path, "adaptive", 40, 0, .2)
            self.assertEqual(result["sequence_offset"], -80)
            self.assertEqual(result["expected_by_sequence"], 8)
            self.assertEqual(result["pairs"], 7)
            self.assertEqual(result["max_us"], 15000)

    def test_measures_from_sensor_time_and_excludes_settling_and_gaps(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "frames.csv"
            with path.open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["unit", "sequence", "sensor_ns", "reported_us", "requested_us", "stage"])
                for unit in ("reference", "follower"):
                    for i, stage in enumerate(("baseline", "finish")):
                        for seq, us in ((0, 0), (1, 75), (2, 100), (4, 160), (5, 185)):
                            writer.writerow([unit, i * 6 + seq, (i * 200 + us) * 1000, 30, 32, stage])
            result = summarize(path, settle_frames=1)
            self.assertEqual(result["follower"]["stages"]["finish"], {
                "intervals": 2, "requested_us": 32, "reported_median_us": 30,
                "measured_mean_us": 25,
            })
            self.assertEqual(result["follower"]["sequence_gaps"], 2)
