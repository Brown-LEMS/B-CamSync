"""External example preserves sensor timing and observes baseline restoration."""

from pathlib import Path
import sys
import unittest

from bcamsync import Pulse, boot_time_ns

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from opencv_sync import CameraTiming


class CameraTimingTests(unittest.TestCase):
    def setUp(self):
        self.unit = CameraTiming("board", 40, 20000, 30000)
        self.unit.bind("camera-test", 1)
        pulse = Pulse()
        pulse.correction_id, pulse.delta_us, pulse.count = 1, 200, 2
        pulse.expires_ns = boot_time_ns() + 1_000_000_000
        self.pulse = pulse

    def test_restore_requires_observed_baseline_and_preserves_sensor_timestamp(self):
        self.unit.adjust(self.pulse)
        first, second, restore = [self.unit.next_submission() for _ in range(3)]
        self.assertEqual([first.duration_us, second.duration_us, restore.duration_us],
                         [25200, 25200, 25000])
        self.assertTrue(restore.restoration)
        sensor_ns = boot_time_ns()
        self.unit.observe(10, sensor_ns, 25200, restore)
        snapshot = self.unit.snapshot()
        self.assertEqual(snapshot.correction.state, "settling")
        self.assertEqual(snapshot.frames[-1].sensor_timestamp_ns, sensor_ns)
        self.assertEqual(snapshot.frames[-1].timestamp_ns, sensor_ns)
        self.assertEqual(snapshot.frames[-1].reported_duration_us, 25200)
        self.unit.observe(11, sensor_ns + 25_016_000, 25016, self.unit.next_submission())
        restored = self.unit.snapshot().correction
        self.assertEqual(restored.state, "restored")
        self.assertEqual(restored.restored_timestamp_ns, sensor_ns + 25_016_000)
        self.unit.bind("camera-test", 2)
        self.assertEqual(len(self.unit.snapshot().frames), 2)

    def test_cancel_restores_baseline_without_owning_capture(self):
        self.unit.adjust(self.pulse)
        self.assertEqual(self.unit.next_submission().duration_us, 25200)
        self.unit.cancel()
        restore = self.unit.next_submission()
        self.assertTrue(restore.restoration)
        self.unit.observe(1, boot_time_ns(), 25000, restore)
        self.assertEqual(self.unit.snapshot().correction.state, "restored")
        self.assertEqual(self.unit.next_submission().duration_us, 25000)
        self.assertEqual(self.unit.fault(), "")


if __name__ == "__main__":
    unittest.main()
