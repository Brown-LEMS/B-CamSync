"""Python configuration and immutable frame ownership checks."""

import gc
from pathlib import Path
import threading
import time
import unittest

import numpy as np

from bcamsync import Config, FakeOptions


ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_all_representative_topologies_load(self):
        expected = {"one_node_two_boards": (1, 2), "two_nodes_one_board": (2, 2), "two_nodes_two_boards": (2, 4)}
        for filename, counts in expected.items():
            with self.subTest(filename=filename):
                config = Config.from_file(ROOT / "docs" / "configs" / f"{filename}.json")
                self.assertEqual((len(config.nodes), len(config.units)), counts)
                self.assertEqual(config.reference_node(), "node-a")
                self.assertEqual(config.fps, 40)
                unit = config.unit(config.reference_unit)
                for field in ("width", "height", "views", "fps"):
                    self.assertFalse(hasattr(unit, field))

    def test_reference_selection_and_invalid_configuration(self):
        config = Config.from_file(ROOT / "docs" / "configs" / "two_nodes_one_board.json")
        config.reference_unit = "board-b1"
        config.validate()
        self.assertEqual(config.reference_node(), "node-b")
        config.reference_unit = "missing"
        with self.assertRaises(ValueError):
            config.validate()

    def test_nested_synthetic_options_are_writable(self):
        config = Config.from_file(ROOT / "tests" / "fixtures" / "configs" / "fake_one_node.json")
        unit = config.unit("board-a2")
        unit.fake = FakeOptions()
        unit.fake.width = 32
        unit.fake.height = 8
        unit.fake.initial_phase_us = 1234
        unit.fake.drift_ppm = 25
        config.units = [config.unit("board-a1"), unit]
        config.validate()
        self.assertEqual(config.unit("board-a2").fake.width, 32)
        self.assertEqual(config.unit("board-a2").fake.initial_phase_us, 1234)


class FrameOwnershipTests(unittest.TestCase):
    def setUp(self):
        # This fixture extension is built only with BCAMSYNC_BUILD_TESTS.
        from bcamsync._test_support import make_packet
        self.packet = make_packet()

    def test_views_share_board_storage_and_have_the_expected_stride(self):
        views = self.packet.views
        self.assertEqual(len(views), 4)
        self.assertEqual(views[0].shape, (3, 2))
        self.assertEqual(views[0].strides, (8, 1))
        np.testing.assert_array_equal(views[0], [[0, 1], [8, 9], [16, 17]])
        np.testing.assert_array_equal(views[3], [[6, 7], [14, 15], [22, 23]])
        self.assertEqual(views[0].dtype, np.uint8)

    def test_slice_retains_storage_after_packet_and_parent_array_are_released(self):
        view = self.packet.view(2)
        fragment = view[1:, 1:]
        del self.packet
        del view
        gc.collect()
        np.testing.assert_array_equal(fragment, [[13], [21]])

    def test_views_are_readonly_and_cannot_be_made_writable(self):
        view = self.packet.view(0)
        self.assertFalse(view.flags.writeable)
        with self.assertRaises(ValueError):
            view[0, 0] = 255
        with self.assertRaises(ValueError):
            view.setflags(write=True)

    def test_invalid_view_index_raises(self):
        with self.assertRaises(IndexError):
            self.packet.view(4)
        with self.assertRaises((TypeError, IndexError)):
            self.packet.view(-1)

    def test_slice_survives_session_shutdown_and_destruction(self):
        from bcamsync._test_support import make_session
        session = make_session()
        with session:
            self.assertTrue(session.wait_ready(2))
            frame = session.read("fixture-board")
            fragment = frame.view(1)[1:, :]
        del frame
        del session
        gc.collect()
        np.testing.assert_array_equal(fragment, [[10, 11], [18, 19]])

    def test_blocking_read_releases_the_gil(self):
        from bcamsync._test_support import make_session
        session = make_session(empty_reads=True)
        with session:
            self.assertTrue(session.wait_ready(2))
            ticks = []
            worker = threading.Timer(0.025, lambda: ticks.append(time.monotonic()))
            worker.start()
            began = time.monotonic()
            self.assertIsNone(session.read("fixture-board", 0.25))
            worker.join(timeout=2)
            self.assertEqual(len(ticks), 1)
            # A timer callback must execute during the native wait, not when
            # the waiting call returns and finally makes the GIL available.
            self.assertLess(ticks[0] - began, 0.15)

    def test_invalid_timeout_is_rejected_before_native_wait(self):
        from bcamsync._test_support import make_session
        session = make_session()
        for duration in (-1, float("nan"), float("inf")):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                session.read("fixture-board", duration)


if __name__ == "__main__":
    unittest.main()
