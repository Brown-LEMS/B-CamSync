import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.experiments.summarize_period_response import summarize


class PeriodResponseSummaryTest(unittest.TestCase):
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
