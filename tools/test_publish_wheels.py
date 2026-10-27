"""Release publication contracts without modifying GitHub or local Git references."""

from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import tomllib
import unittest
from unittest.mock import patch
import zipfile

import publish_wheels as publisher


class PublicationTests(unittest.TestCase):
    @contextmanager
    def wheels(self, version="0.2.0"):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for minor in range(11, 16):
                wheel = root / f"bcamsync-{version}-cp3{minor}-cp3{minor}-linux_aarch64.whl"
                with zipfile.ZipFile(wheel, "w") as archive:
                    archive.writestr(f"bcamsync-{version}.dist-info/METADATA", f"Name: bcamsync\nVersion: {version}\n")
            yield root

    def test_version_input_normalizes_v_prefix_and_rejects_non_versions(self):
        for value, version in (("", ""), ("0.2.0", "0.2.0"), ("v1.2.3", "1.2.3")):
            self.assertEqual(publisher.release_version(value), version)
        for value in ("rolling", "../bad", "1.2", "01.2.3", "1.2.3\n", "1.2.3; command"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                publisher.release_version(value)

    def test_incomplete_matrix_or_wrong_wheel_metadata_blocks_publication(self):
        with self.wheels() as root:
            publisher.checked_wheels(root, "0.2.0")
            wheel = next(root.glob("*.whl"))
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("bcamsync-0.2.0.dist-info/METADATA", "Name: bcamsync\nVersion: 9.9.9\n")
            with self.assertRaisesRegex(RuntimeError, "metadata"):
                publisher.checked_wheels(root, "0.2.0")
            wheel.unlink()
            with self.assertRaisesRegex(RuntimeError, "exactly the five"):
                publisher.checked_wheels(root, "0.2.0")

    def commands(self, existing, *, version="", tag_commit="abc", fail_upload=False):
        calls = []
        self.last_calls = calls
        notes = []
        package_version = version or tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"]["version"]

        def github(*args):
            calls.append(args)
            if "--notes-file" in args:
                notes.append(Path(args[args.index("--notes-file") + 1]).read_text())
            if args[:2] == ("release", "upload") and fail_upload:
                raise subprocess.CalledProcessError(1, ["gh", *args], stderr="upload failed")
            return ""

        with self.wheels(package_version) as root, patch.object(publisher, "existing_release", return_value=existing), \
                patch.object(publisher, "github", side_effect=github), \
                patch.object(publisher.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 1 if tag_commit is None else 0, stdout=(tag_commit or "") + "\n")):
            publisher.publish("owner/repo", "abc", version, root, "https://github.com/build/42")
        return calls, notes

    def test_initial_rolling_release_is_draft_until_all_assets_and_tag_are_ready(self):
        calls, notes = self.commands(None)
        self.assertEqual(calls[0][:3], ("release", "create", "rolling"))
        self.assertIn("--draft", calls[0])
        self.assertNotIn("--clobber", calls[1])
        self.assertIn("repos/owner/repo/git/refs/tags/rolling", calls[2])
        self.assertIn("force=true", calls[2])
        self.assertEqual(calls[-1][:3], ("release", "edit", "rolling"))
        self.assertIn("--draft=false", calls[-1])
        self.assertIn("--prerelease=true", calls[-1])
        self.assertTrue(all("Commit: abc" in note and "build/42" in note for note in notes))

    def test_first_rolling_build_creates_tag_before_draft_release(self):
        calls, _ = self.commands(None, tag_commit=None)
        self.assertEqual(calls[0][:3], ("api", "--method", "POST"))
        self.assertIn("ref=refs/tags/rolling", calls[0])
        self.assertEqual(calls[1][:3], ("release", "create", "rolling"))
        self.assertIn("--draft=false", calls[-1])

    def test_published_rolling_update_replaces_assets_without_unpublishing_or_republishing(self):
        existing = dict(id=7, draft=False, assets=[dict(id=11, name="obsolete.whl")], immutable=False)
        calls, notes = self.commands(existing)
        self.assertEqual(calls[0][:3], ("release", "upload", "rolling"))
        self.assertFalse(any(call[:2] == ("release", "create") for call in calls))
        self.assertFalse(any(arg.startswith("--draft") for call in calls for arg in call))
        self.assertIn("--clobber", calls[0])
        self.assertIn("repos/owner/repo/releases/assets/11", calls[1])
        self.assertIn("repos/owner/repo/git/refs/tags/rolling", calls[2])
        self.assertEqual(calls[-1][:3], ("release", "edit", "rolling"))
        self.assertIn("--prerelease=true", calls[-1])
        self.assertIn("Commit: abc", notes[-1])

    def test_existing_initial_rolling_draft_is_published_after_upload(self):
        calls, _ = self.commands(dict(id=7, draft=True, assets=[], immutable=False))
        self.assertEqual(calls[0][:3], ("release", "upload", "rolling"))
        self.assertFalse(any("--draft=true" in call for call in calls))
        self.assertIn("--draft=false", calls[-1])

    def test_failed_in_place_upload_does_not_hide_release_or_change_tag_and_notes(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.commands(dict(id=7, draft=False, assets=[], immutable=False), fail_upload=True)
        self.assertEqual(len(self.last_calls), 1)
        self.assertEqual(self.last_calls[0][:3], ("release", "upload", "rolling"))
        self.assertIn("--clobber", self.last_calls[0])

    def test_versioned_release_uses_fixed_tag_and_never_clobbers_assets_or_moves_tags(self):
        calls, _ = self.commands(None, version="0.2.0")
        self.assertEqual(calls[0][:3], ("release", "create", "v0.2.0"))
        self.assertNotIn("--clobber", calls[1])
        self.assertFalse(any("PATCH" in call for call in calls))
        self.assertIn("--prerelease=false", calls[-1])
        self.assertIn("--latest=true", calls[-1])

    def test_existing_version_immutable_rolling_or_conflicting_tag_never_mutates_github(self):
        for existing, version, tag_commit, message in (
                (dict(id=1), "0.2.0", "abc", "never overwritten"),
                (dict(id=2, immutable=True), "", "abc", "immutable"),
                (None, "0.2.0", "different", "different commit")):
            with self.subTest(message=message):
                with self.assertRaisesRegex(RuntimeError, message):
                    self.commands(existing, version=version, tag_commit=tag_commit)
                self.assertEqual(self.last_calls, [])

    def test_failed_upload_does_not_publish_draft(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.commands(None, fail_upload=True)
        self.assertEqual([call[:2] for call in self.last_calls], [("release", "create"), ("release", "upload")])
        self.assertFalse(any("--draft=false" in call for call in self.last_calls))

    def test_existing_drafts_are_found_across_release_pages_and_api_errors_propagate(self):
        draft = dict(tag_name="rolling", draft=True, id=7)
        with patch.object(publisher, "github", return_value=json.dumps([[dict(tag_name="v0.2.0")], [draft]])) as github:
            self.assertEqual(publisher.existing_release("owner/repo", "rolling"), draft)
            self.assertIsNone(publisher.existing_release("owner/repo", "missing"))
            self.assertIn("--paginate", github.call_args.args)
        with patch.object(publisher, "github", side_effect=subprocess.CalledProcessError(
                1, ["gh"], stderr="HTTP 403")):
            with self.assertRaises(subprocess.CalledProcessError):
                publisher.existing_release("owner/repo", "rolling")


if __name__ == "__main__":
    unittest.main()
