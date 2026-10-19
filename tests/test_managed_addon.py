"""The separately installed capture add-on preserves explicit legacy imports."""

import unittest

import bcamsync
import bcamsync_managed


class ManagedAddonTests(unittest.TestCase):
    def test_legacy_imports_resolve_to_the_addon(self):
        from bcamsync import Config, FakeOptions, FramePacket, Session, UnitConfig
        self.assertIs(Config, bcamsync_managed.Config)
        self.assertIs(FakeOptions, bcamsync_managed.FakeOptions)
        self.assertIs(FramePacket, bcamsync_managed.FramePacket)
        self.assertIs(Session, bcamsync_managed.Session)
        self.assertIs(UnitConfig, bcamsync_managed.UnitConfig)

    def test_managed_config_projects_the_core_contract(self):
        from pathlib import Path
        config = bcamsync_managed.Config.from_file(
            Path(__file__).resolve().parents[1] / "examples" / "configs" / "fake_one_node.json")
        sync = config.sync_config()
        self.assertIsInstance(sync, bcamsync.SyncConfig)
        self.assertIsInstance(sync.nodes[0], bcamsync.NodeConfig)
        self.assertEqual(sync.sync_json(), config.sync_json())
        self.assertIsNone(config.join_timeout_seconds)
        config.join_timeout_seconds = 120.5
        self.assertEqual(config.sync_config().join_timeout_seconds, 120.5)
        config.join_timeout_seconds = None
        self.assertIsNone(config.sync_config().join_timeout_seconds)


if __name__ == "__main__":
    unittest.main()
