"""Anonymous Release selection, streamed downloads, and installation contracts."""

from contextlib import ExitStack, contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
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
        stack.enter_context(patch.dict(installer.os.environ, {"CONDA_PREFIX": "", "GH_TOKEN": "must-not-be-sent"}))
        return stack

    @contextmanager
    def release_server(self):
        payload = b"test wheel data" * 100_000
        abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
        state = dict(payload=payload, requests=[], status=200)
        asset = dict(name=f"bcamsync-0.2.0-{abi}-{abi}-linux_aarch64.whl", size=len(payload),
                     digest=f"sha256:{hashlib.sha256(payload).hexdigest()}")
        release = dict(tag_name="rolling", html_url="https://github.com/owner/repo/releases/tag/rolling",
                       target_commitish="abc123", draft=False, assets=[asset])
        state["release"] = release

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                state["requests"].append((self.path, dict(self.headers)))
                data = state["payload"] if self.path == "/wheel" else json.dumps(release).encode()
                code = 200 if self.path == "/wheel" else state["status"]
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            base = f"http://127.0.0.1:{server.server_port}"
            asset["browser_download_url"] = base + "/wheel"
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            try:
                with patch.object(installer, "API_ROOT", base):
                    yield state
            finally:
                server.shutdown()
                thread.join()

    def test_target_matches_current_abi_on_arm64(self):
        abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
        with self.environment():
            self.assertEqual(installer.wheel_target(), f"-{abi}-{abi}-linux_aarch64.whl")

    def test_unsupported_environments_fail_before_contacting_github(self):
        for changes in ({"system": "Darwin"}, {"machine": "armv7l"}, {"machine": "x86_64"},
                        {"libc": ("musl", "1.2")}, {"libc": ("glibc", "2.35")},
                        {"free_threaded": True}, {"debug": True}, {"prefix": "/usr", "base_prefix": "/usr"}):
            with self.subTest(changes=changes), self.environment(**changes), \
                    patch.object(installer, "request") as request:
                with self.assertRaises(RuntimeError):
                    installer.main([])
                request.assert_not_called()

    def test_default_rolling_download_is_anonymous_and_needs_no_cli(self):
        with self.environment(), self.release_server() as state, TemporaryDirectory() as directory, \
                patch.object(installer.subprocess, "run") as command:
            installer.main(["--repo", "owner/repo", "--download-only", directory])
            asset = state["release"]["assets"][0]
            self.assertEqual((Path(directory) / asset["name"]).read_bytes(), state["payload"])
            self.assertEqual([path for path, _ in state["requests"]],
                             ["/repos/owner/repo/releases/tags/rolling", "/wheel"])
            for _, headers in state["requests"]:
                self.assertNotIn("Authorization", headers)
            command.assert_not_called()

    def test_versioned_release_is_requested_by_exact_tag(self):
        with self.environment(), self.release_server() as state, TemporaryDirectory() as directory:
            installer.main(["--repo", "owner/repo", "--release", "v0.2.0", "--download-only", directory])
            self.assertEqual(state["requests"][0][0], "/repos/owner/repo/releases/tags/v0.2.0")

    def test_missing_or_draft_release_does_not_try_another_source(self):
        for draft in (False, True):
            with self.subTest(draft=draft), self.environment(), self.release_server() as state, \
                    patch.object(installer.subprocess, "run") as command:
                state["status"] = 200 if draft else 404
                state["release"]["draft"] = draft
                with self.assertRaisesRegex(RuntimeError, "unavailable|not been published"):
                    installer.main(["--repo", "owner/repo"])
                self.assertEqual(len(state["requests"]), 1)
                command.assert_not_called()

    def test_wrong_abi_or_ambiguous_assets_are_not_downloaded(self):
        for count in (0, 2):
            with self.subTest(count=count), self.environment(), self.release_server() as state:
                state["release"]["assets"] *= count
                with self.assertRaisesRegex(RuntimeError, f"found {count}"):
                    installer.main(["--repo", "owner/repo"])
                self.assertEqual(len(state["requests"]), 1)

    def test_missing_digest_corruption_and_truncation_prevent_installation(self):
        for failure in ("digest", "corrupt", "truncated"):
            with self.subTest(failure=failure), self.environment(), self.release_server() as state, \
                    patch.object(installer.subprocess, "run") as command:
                asset = state["release"]["assets"][0]
                if failure == "digest":
                    asset.pop("digest")
                elif failure == "corrupt":
                    state["payload"] = b"x" * len(state["payload"])
                else:
                    state["payload"] = state["payload"][:10]
                with self.assertRaisesRegex(RuntimeError, "digest|SHA-256"):
                    installer.main(["--repo", "owner/repo"])
                command.assert_not_called()

    def test_install_uses_running_interpreter_and_verifies_imports(self):
        with self.environment(), self.release_server() as state:
            calls = []

            def run(args, **kwargs):
                calls.append(args)
                if "pip" in args:
                    self.assertEqual(Path(args[-1]).read_bytes(), state["payload"])
                return subprocess.CompletedProcess(args, 0)

            with patch.object(installer.subprocess, "run", side_effect=run):
                installer.main(["--repo", "owner/repo"])
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][:4], [sys.executable, "-m", "pip", "install"])
            for flag in ("--no-index", "--no-deps", "--force-reinstall"):
                self.assertIn(flag, calls[0])
            self.assertIn("from bcamsync import SyncConfig, SyncSession", calls[1][-1])
            self.assertFalse(Path(calls[0][-1]).exists())


if __name__ == "__main__":
    unittest.main()
