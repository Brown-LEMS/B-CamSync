"""Pure software checks for provisioning plans; no host configuration is changed."""

import copy
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bcamsync_platform", ROOT / "python" / "bcamsync" / "platform.py")
platform = importlib.util.module_from_spec(spec)
spec.loader.exec_module(platform)


class PlatformPlanTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "examples" / "configs" / "two_nodes_one_board.json").read_text())

    def test_static_link_has_no_default_route_or_dns(self):
        plan = platform.make_plan(self.config, "node-a", "researcher")
        command = plan["network_commands"][0]
        self.assertEqual(command[command.index("ipv4.addresses") + 1], "10.77.0.1/30")
        self.assertEqual(command[command.index("ipv4.never-default") + 1], "yes")
        self.assertEqual(command[command.index("ipv4.ignore-auto-dns") + 1], "yes")
        self.assertNotIn("ipv4.gateway", command)
        self.assertNotIn("ipv4.dns", command)

    def test_reference_change_preserves_addresses_and_changes_clock_role(self):
        original = platform.make_plan(self.config, "node-a")
        self.config["reference_unit"] = "board-b1"
        changed = platform.make_plan(self.config, "node-a")
        self.assertEqual(original["network_commands"], changed["network_commands"])
        self.assertIn("local stratum 8", original["chrony_config"])
        self.assertIn("server 10.77.0.2", changed["chrony_config"])
        self.assertNotIn("local stratum", changed["chrony_config"])

    def test_existing_network_generates_no_network_mutations(self):
        self.config["network"] = {"managed": False}
        self.assertEqual(platform.make_plan(self.config, "node-b")["network_commands"], [])

    def test_single_node_needs_no_provisioning(self):
        config = json.loads((ROOT / "examples" / "configs" / "one_node_two_boards.json").read_text())
        plan = platform.make_plan(config, "node-a")
        self.assertEqual(plan["network_commands"], [])
        self.assertIsNone(plan["chrony_config"])
        self.assertEqual(plan["clock_prepare_command"], [])

    def test_fake_nodes_share_localhost_and_skip_all_physical_setup(self):
        config = json.loads((ROOT / "examples" / "configs" / "fake_two_nodes.json").read_text())
        config["network"] = {"managed": True, "subnet": "10.77.0.0/30"}
        plan = platform.make_plan(config, "node-b", "researcher")
        self.assertEqual(plan["backend"], "fake")
        self.assertFalse(plan["managed_network"])
        self.assertEqual(plan["network_commands"], [])
        self.assertFalse(plan["clock_managed"])
        self.assertIsNone(plan["chrony_config"])
        self.assertIsNone(plan["sudoers"])
        self.assertEqual(plan["clock_prepare_command"], [])

    def test_fake_duplicate_endpoint_is_rejected(self):
        config = json.loads((ROOT / "examples" / "configs" / "fake_two_nodes.json").read_text())
        config["nodes"][1]["port"] = config["nodes"][0]["port"]
        with self.assertRaises(ValueError):
            platform.make_plan(config, "node-a")

    def test_clock_helper_is_fixed_and_limits_stepping_to_startup(self):
        plan = platform.make_plan(self.config, "node-b", "researcher")
        self.assertIn('[ "$#" -eq 0 ]', plan["clock_helper_script"])
        self.assertIn("makestep 0.000100000 1", plan["clock_helper_script"])
        self.assertIn("makestep 0 0", plan["clock_helper_script"])
        self.assertIn("/usr/bin/timeout -k 1s", plan["clock_helper_script"])
        self.assertNotIn("makestep", plan["chrony_config"])
        self.assertTrue(plan["sudoers"].endswith(' ""\n'))

    def test_invalid_and_duplicate_addresses_are_rejected(self):
        for address in ("10.77.0.0", "10.77.0.3", "10.77.0.1", "192.168.1.2"):
            config = copy.deepcopy(self.config)
            config["nodes"][1]["address"] = address
            with self.subTest(address=address), self.assertRaises(ValueError):
                platform.make_plan(config, "node-a")

    def test_identifiers_cannot_escape_helper_path(self):
        self.config["group_id"] = "../../tmp/command"
        with self.assertRaises(ValueError):
            platform.make_plan(self.config, "node-a")

    def test_identifiers_cannot_generate_ignored_sudoers_names(self):
        self.config["group_id"] = "lab.v1"
        with self.assertRaises(ValueError):
            platform.make_plan(self.config, "node-a")

    def test_clock_check_requires_selected_source_and_fresh_observation(self):
        plan = platform.make_plan(self.config, "node-b")
        report = "\n".join([
            "Reference ID : 0A4D0001 (10.77.0.1)", "Stratum : 9",
            "Ref time (UTC) : Mon Oct 12 20:00:00 2026",
            "System time : 0.00001 seconds slow of NTP time", "Last offset : -0.00001 seconds",
            "Leap status : Normal",
        ])
        platform.validate_tracking(plan, report, now=1791835205)
        with self.assertRaises(RuntimeError):
            platform.validate_tracking(plan, report, now=1791835220)
        with self.assertRaises(RuntimeError):
            platform.validate_tracking(plan, report.replace("0A4D0001", "C0A80101"), now=1791835205)


if __name__ == "__main__":
    unittest.main()
