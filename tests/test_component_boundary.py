"""The synchronization API imports and runs adapters without the managed add-on."""

from pathlib import Path
import importlib.util
import os
import subprocess
import sys
import unittest


class ComponentBoundaryTests(unittest.TestCase):
    def test_core_and_opencv_adapter_do_not_import_capture_dependencies(self):
        example = Path(__file__).resolve().parents[1] / "examples" / "opencv_sync.py"
        code = '''
import importlib.abc
import importlib.util
from pathlib import Path
import sys

class NoCaptureDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"numpy", "libcamera", "cv2", "bcamsync_managed"}:
            raise AssertionError("Core imported capture dependency: " + fullname)

sys.meta_path.insert(0, NoCaptureDependencies())
import bcamsync
from bcamsync import SyncConfig, SyncSession, SyncUnit, PhaseActuator
from bcamsync import adapters
from bcamsync.adapters.libcamera import CameraTiming
assert adapters.SyncUnit is SyncUnit
assert adapters.PhaseActuator is PhaseActuator
assert not hasattr(bcamsync._native, "Session")
assert not hasattr(bcamsync._native, "FramePacket")
spec = importlib.util.spec_from_file_location("opencv_sync", sys.argv[1])
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)
assert issubclass(example.CameraTiming, SyncUnit)
assert example.CameraTiming is CameraTiming
from bcamsync.management import NodeService
from bcamsync.http_management import HttpNodeService
from bcamsync.node import run_node
config = example.sync_config()
assert config.join_timeout_seconds is None
config.join_timeout_seconds = 120.5
config.validate()
assert config.join_timeout_seconds == 120.5
for invalid in (0, -1, float("nan"), float("inf")):
    config.join_timeout_seconds = invalid
    try:
        config.validate()
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid join timeout was accepted")
config.join_timeout_seconds = None
adapters = {unit.id: example.CameraTiming(unit.id, config.fps, 1000, 1000000)
            for unit in config.units}
with_controller = SyncSession(config, "pi5", adapters)
status = with_controller.status()
assert status.waiting_nodes == []
assert status.stop_node == ""
assert status.stop_reason == ""
controller = NodeService(config, "pi5", session=with_controller)
assert controller.request("status")["state"] == "stopped"
assert controller.request("configuration")["node_id"] == "pi5"
controller.close()
actuator = PhaseActuator()
actuator.bind("boundary", 1)
assert actuator.next(25000, 1).duration_us == 25000
'''
        environment = os.environ.copy()
        core = importlib.util.find_spec("bcamsync")
        environment["PYTHONPATH"] = str(Path(core.origin).resolve().parent.parent)
        result = subprocess.run([sys.executable, "-S", "-c", code, str(example)],
                                text=True, capture_output=True, env=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
