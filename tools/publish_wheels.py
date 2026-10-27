#!/usr/bin/env python3
"""Publish a complete tested ARM64 wheel set; invoked by the manual Actions workflow."""

import argparse
from email import message_from_bytes
import json
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib
import zipfile


def release_version(value):
    """Accept an optional X.Y.Z version, with or without the v tag prefix."""
    if not value:
        return ""
    version = value.removeprefix("v")
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError("Version must be X.Y.Z or vX.Y.Z; leave it empty for rolling.")
    return version


def github(*args):
    return subprocess.run(["gh", *args], check=True, text=True, capture_output=True).stdout


def existing_release(repo, tag):
    # Authenticated listing also finds drafts left by an interrupted initial publication.
    pages = json.loads(github("api", "--paginate", "--slurp", f"repos/{repo}/releases?per_page=100"))
    return next((release for page in pages for release in page if release["tag_name"] == tag), None)


def checked_wheels(directory, version):
    expected = {f"bcamsync-{version}-cp3{minor}-cp3{minor}-linux_aarch64.whl" for minor in range(11, 16)}
    paths = sorted(directory.glob("*.whl"))
    if {path.name for path in paths} != expected:
        raise RuntimeError("Publication requires exactly the five matching CPython 3.11–3.15 ARM64 core wheels.")
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            metadata = message_from_bytes(archive.read(f"bcamsync-{version}.dist-info/METADATA"))
        if metadata["Name"] != "bcamsync" or metadata["Version"] != version:
            raise RuntimeError(f"Wheel metadata does not match the release version: {path.name}")
    return paths


def publish(repo, commit, version, directory, run_url):
    tag = f"v{version}" if version else "rolling"
    package_version = version or tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"]["version"]
    wheels = checked_wheels(directory, package_version)
    existing = existing_release(repo, tag)
    if existing and version:
        raise RuntimeError(f"Release {tag} already exists; versioned releases are never overwritten.")
    if existing and existing.get("immutable"):
        raise RuntimeError("The rolling release is immutable. Rolling publication requires mutable GitHub Releases.")
    # The checkout includes tags. An existing version tag may be reused only for its original commit.
    reference = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}"],
                               text=True, capture_output=True)
    if reference.returncode not in (0, 1):
        raise RuntimeError(f"Cannot inspect the release tag: {reference.stderr.strip()}")
    if version and reference.returncode == 0 and reference.stdout.strip() != commit:
        raise RuntimeError(f"Tag {tag} points to a different commit; it will not be moved.")
    title = f"B-CamSync {tag}" if version else "B-CamSync rolling ARM64 wheels"
    notes = f"Commit: {commit}\n\nBuild: {run_url}\n\nCPython 3.11–3.15 on Linux ARM64 (glibc >= 2.36).\n"
    if not version:
        notes += "\nThis rolling prerelease is replaced by each successful unversioned manual build.\n"
    print(f"Publishing {tag} from {commit}", flush=True)
    if not version and reference.returncode == 1:
        # A draft Release does not guarantee that its tag has been created yet.
        github("api", "--method", "POST", f"repos/{repo}/git/refs",
               "-f", "ref=refs/tags/rolling", "-f", f"sha={commit}")
    with tempfile.TemporaryDirectory(prefix="bcamsync-release-") as temporary:
        body = Path(temporary) / "notes.md"
        body.write_text(notes)
        if not existing:
            github("release", "create", tag, "--repo", repo, "--target", commit,
                   "--title", title, "--notes-file", str(body), "--draft", "--latest=false")
        upload = ["release", "upload", tag, "--repo", repo, *map(str, wheels)]
        if existing:
            upload.append("--clobber")
        github(*upload)
        if existing:
            names = {wheel.name for wheel in wheels}
            for asset in existing["assets"]:
                if asset["name"] not in names:
                    github("api", "--method", "DELETE", f"repos/{repo}/releases/assets/{asset['id']}")
        if not version:
            github("api", "--method", "PATCH", f"repos/{repo}/git/refs/tags/rolling",
                   "-f", f"sha={commit}", "-F", "force=true")
        edit = ["release", "edit", tag, "--repo", repo, "--target", commit,
                "--title", title, "--notes-file", str(body), "--prerelease=false" if version else "--prerelease=true",
                "--latest=true" if version else "--latest=false"]
        if not existing or existing["draft"]:
            edit.append("--draft=false")
        github(*edit)
    print(f"Published https://github.com/{repo}/releases/tag/{tag}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--version", default="", type=release_version)
    parser.add_argument("--wheels", type=Path, required=True)
    parser.add_argument("--run-url", required=True)
    args = parser.parse_args()
    publish(args.repo, args.commit, args.version, args.wheels, args.run_url)


if __name__ == "__main__":
    main()
