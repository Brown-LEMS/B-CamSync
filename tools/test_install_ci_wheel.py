"""CI installer selection, installation, and failure contracts without GitHub access."""

from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import install_ci_wheel as installer


class InstallerTests(unittest.TestCase):
    def environment(self, **changes):
        values = dict(system="Linux", machine="aarch64", libc=("glibc", "2.36"),
                      prefix="/project/.venv", base_prefix="/usr", debug=False, free_threaded=False)
        values.update(changes)
        stack = ExitStack()
        stack.enter_context(patch.object(installer.platform, "system", return_value=values["system"]))
        stack.enter_context(patch.object(installer.platform, "machine", return_value=values["machine"]))
        stack.enter_context(patch.object(installer.platform, "libc_ver", return_value=values["libc"]))
        stack.enter_context(patch.object(installer.sys, "prefix", values["prefix"]))
        stack.enter_context(patch.object(installer.sys, "base_prefix", values["base_prefix"]))
        stack.enter_context(patch.object(installer.sysconfig, "get_config_var", side_effect=lambda key: {
            "Py_DEBUG": values["debug"], "Py_GIL_DISABLED": values["free_threaded"]}[key]))
        stack.enter_context(patch.dict(installer.os.environ, {"CONDA_PREFIX": ""}))
        return stack

    def test_target_matches_current_abi_and_supported_architectures(self):
        minor = f"{sys.version_info.major}.{sys.version_info.minor}"
        abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
        for arch in ("aarch64", "x86_64"):
            with self.subTest(arch=arch), self.environment(machine=arch):
                self.assertEqual(installer.wheel_target(),
                                 (f"wheels-cpython{minor}-linux_{arch}", f"-{abi}-{abi}-linux_{arch}.whl"))

    def test_unsupported_environments_fail_before_contacting_github(self):
        for changes in ({"system": "Darwin"}, {"machine": "armv7l"}, {"libc": ("musl", "1.2")},
                        {"libc": ("glibc", "2.35")}, {"free_threaded": True}, {"debug": True},
                        {"prefix": "/usr", "base_prefix": "/usr"}):
            with self.subTest(changes=changes), self.environment(**changes), \
                    patch.object(installer, "github") as github:
                with self.assertRaises(RuntimeError):
                    installer.main([])
                github.assert_not_called()

    def test_latest_run_selects_only_successful_branch_pushes(self):
        run = dict(databaseId=42, headSha="abc", url="https://github.com/build/42")
        with patch.object(installer.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, stdout=json.dumps([run]))) as command:
            self.assertEqual(installer.latest_run("owner/repo", "experiment"), run)
        args = command.call_args.args[0]
        for flag, value in (("--workflow", "native.yml"), ("--branch", "experiment"),
                            ("--event", "push"), ("--status", "success"), ("--limit", "1")):
            self.assertEqual(args[args.index(flag) + 1], value)
        with patch.object(installer, "github", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "No successful"):
                installer.latest_run("owner/repo", "main")

    def test_missing_expired_and_paginated_artifacts(self):
        with patch.object(installer, "github", return_value={"artifacts": []}):
            with self.assertRaisesRegex(RuntimeError, "has no matching"):
                installer.require_artifact("owner/repo", 42, "matching")
        with patch.object(installer, "github", return_value={"artifacts": [dict(name="matching", expired=True)]}):
            with self.assertRaisesRegex(RuntimeError, "expired"):
                installer.require_artifact("owner/repo", 42, "matching")
        with patch.object(installer, "github", side_effect=[
                {"artifacts": [dict(name="other", expired=False)] * 100},
                {"artifacts": [dict(name="matching", expired=False)]}]) as github:
            installer.require_artifact("owner/repo", 42, "matching")
            self.assertIn("page=2", github.call_args.args[1])

    def test_wrong_abi_or_ambiguous_wheels_are_never_installed(self):
        with TemporaryDirectory() as directory, patch.object(installer.subprocess, "run"):
            root = Path(directory)
            (root / "bcamsync-0.1.0-cp310-cp310-linux_aarch64.whl").touch()
            suffix = "-cp311-cp311-linux_aarch64.whl"
            with self.assertRaisesRegex(RuntimeError, "found 0"):
                installer.download_wheel("owner/repo", 42, "artifact", suffix, root)
            for version in ("0.1.0", "0.2.0"):
                (root / f"bcamsync-{version}{suffix}").touch()
            with self.assertRaisesRegex(RuntimeError, "found 2"):
                installer.download_wheel("owner/repo", 42, "artifact", suffix, root)

    def execute(self, argv):
        calls = []
        installed = []
        artifact, suffix = installer.wheel_target()

        def command(args, **kwargs):
            calls.append(args)
            if args[:3] == ["gh", "run", "list"]:
                data = [dict(databaseId=42, headSha="abc", url="https://github.com/build/42")]
            elif args[:2] == ["gh", "api"]:
                data = {"artifacts": [dict(name=artifact, expired=False)]}
            elif args[:3] == ["gh", "run", "download"]:
                directory = Path(args[args.index("--dir") + 1])
                # Old GCC artifacts had nested paths; selection does not depend on ZIP layout.
                (directory / "packages").mkdir()
                (directory / "packages" / f"bcamsync-0.1.0{suffix}").write_bytes(b"test wheel")
                data = None
            elif args[1:4] == ["-m", "pip", "install"]:
                installed.append(Path(args[-1]).read_bytes())
                data = None
            else:
                self.assertEqual(args[:2], [sys.executable, "-c"])
                data = None
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(data))

        with patch.object(installer.shutil, "which", return_value="/usr/bin/gh"), \
                patch.object(installer.subprocess, "run", side_effect=command):
            self.assertEqual(installer.main(argv), 0)
        return calls, installed, suffix

    def test_installs_only_core_into_running_interpreter_and_checks_imports(self):
        with self.environment():
            calls, installed, _ = self.execute([])
        self.assertEqual(installed, [b"test wheel"])
        pip = next(args for args in calls if args[1:4] == ["-m", "pip", "install"])
        self.assertEqual(pip[0], sys.executable)
        for flag in ("--no-index", "--no-deps", "--force-reinstall"):
            self.assertIn(flag, pip)
        self.assertIn("from bcamsync import SyncConfig, SyncSession", calls[-1][-1])
        self.assertFalse(Path(pip[-1]).exists(), "Temporary downloads must be removed")

    def test_download_only_preserves_wheel_without_invoking_pip(self):
        with TemporaryDirectory() as directory, self.environment():
            calls, installed, suffix = self.execute(["--download-only", directory])
            self.assertEqual(installed, [])
            self.assertEqual((Path(directory) / f"bcamsync-0.1.0{suffix}").read_bytes(), b"test wheel")
            self.assertFalse(any(args[1:3] == ["-m", "pip"] for args in calls))

    def test_authentication_errors_propagate_without_trying_another_build(self):
        with self.environment(), patch.object(installer.shutil, "which", return_value="/usr/bin/gh"), \
                patch.object(installer.subprocess, "run", side_effect=subprocess.CalledProcessError(
                    1, ["gh"], stderr="authentication required")) as command:
            with self.assertRaises(subprocess.CalledProcessError):
                installer.main([])
            self.assertEqual(command.call_count, 1)


if __name__ == "__main__":
    unittest.main()
