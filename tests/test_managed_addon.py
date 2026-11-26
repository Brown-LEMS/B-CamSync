"""The separately installed capture add-on preserves explicit legacy imports."""

from pathlib import Path
import subprocess
import sys
import unittest

import bcamsync
import bcamsync_managed

ROOT = Path(__file__).resolve().parents[1]


class ManagedAddonTests(unittest.TestCase):
    def test_legacy_imports_resolve_to_the_addon(self):
        from bcamsync import Config, FakeOptions, FramePacket, Session, UnitConfig
        self.assertIs(Config, bcamsync_managed.Config)
        self.assertIs(FakeOptions, bcamsync_managed.FakeOptions)
        self.assertIs(FramePacket, bcamsync_managed.FramePacket)
        self.assertIs(Session, bcamsync_managed.Session)
        self.assertIs(UnitConfig, bcamsync_managed.UnitConfig)

    def test_managed_config_projects_the_core_contract(self):
        config = bcamsync_managed.Config.from_file(
            ROOT / "tests" / "fixtures" / "configs" / "fake_one_node.json")
        sync = config.sync_config()
        self.assertIsInstance(sync, bcamsync.SyncConfig)
        self.assertIsInstance(sync.nodes[0], bcamsync.NodeConfig)
        self.assertEqual(sync.sync_json(), config.sync_json())
        self.assertIsNone(config.join_timeout_seconds)
        config.join_timeout_seconds = 120.5
        self.assertEqual(config.sync_config().join_timeout_seconds, 120.5)
        config.join_timeout_seconds = None
        self.assertIsNone(config.sync_config().join_timeout_seconds)

    def test_capture_cli_frame_statistics_require_verbose(self):
        command = [sys.executable, str(ROOT / "tools/managed/capture.py"),
                   str(ROOT / "tests/fixtures/configs/fake_one_node.json"), "node-a", "--seconds", "0.25"]
        for flags in ([], ["-v"]):
            with self.subTest(flags=flags):
                completed = subprocess.run(command + flags, capture_output=True, text=True, timeout=10)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("Consuming local frames", completed.stdout)
                self.assertEqual("sequence=" in completed.stdout, bool(flags))

    def test_capture_cli_terminal_error_is_brief_unless_verbose(self):
        command = [sys.executable, str(ROOT / "tools/managed/capture.py"), "missing-camera-config.json", "node-a"]
        for flags in ([], ["--verbose"]):
            with self.subTest(flags=flags):
                completed = subprocess.run(command + flags, capture_output=True, text=True, timeout=10)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("missing-camera-config.json", completed.stderr)
                self.assertEqual("Traceback" in completed.stderr, bool(flags))


if __name__ == "__main__":
    unittest.main()
