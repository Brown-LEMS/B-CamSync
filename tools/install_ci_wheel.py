#!/usr/bin/env python3
"""Anonymously install an ARM64 core wheel from a public GitHub Release."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

API_ROOT = "https://api.github.com"


def progress(message):
    print(message, flush=True)


def request(url, accept="application/vnd.github+json"):
    return urlopen(Request(url, headers={"Accept": accept, "User-Agent": "B-CamSync-installer"}), timeout=30)


def wheel_target():
    """Match the ordinary CPython ABI and Bookworm-compatible ARM64 target."""
    if sys.prefix == sys.base_prefix and os.environ.get("CONDA_PREFIX") != sys.prefix:
        raise RuntimeError("Run this script with a project venv or Conda interpreter.")
    if sys.implementation.name != "cpython" or sys.version_info < (3, 11):
        raise RuntimeError("Prebuilt wheels require CPython 3.11 or later.")
    if sysconfig.get_config_var("Py_GIL_DISABLED") or sysconfig.get_config_var("Py_DEBUG"):
        raise RuntimeError("Prebuilt wheels require a standard, non-debug CPython build with the GIL.")
    if platform.system() != "Linux" or platform.machine() != "aarch64":
        raise RuntimeError("Prebuilt wheels support Linux aarch64.")
    libc, version = platform.libc_ver()
    if libc != "glibc" or tuple(map(int, version.split("."))) < (2, 36):
        raise RuntimeError("Prebuilt wheels require glibc 2.36 or later (Debian 12 / Bookworm).")
    abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
    return f"-{abi}-{abi}-linux_aarch64.whl"


def release_wheel(repo, tag, suffix):
    url = f"{API_ROOT}/repos/{repo}/releases/tags/{quote(tag, safe='')}"
    try:
        with request(url) as response:
            release = json.load(response)
    except HTTPError as error:
        error.close()
        if error.code == 404:
            raise RuntimeError(f"Public release {repo}@{tag} is unavailable. It must be published first.") from error
        raise
    if release["draft"]:
        raise RuntimeError(f"Release {tag} has not been published.")
    assets = [asset for asset in release["assets"] if asset["name"].startswith("bcamsync-")
              and asset["name"].endswith(suffix) and Path(asset["name"]).name == asset["name"]]
    if len(assets) != 1:
        raise RuntimeError(f"Expected one compatible core wheel in release {tag}; found {len(assets)}.")
    return release, assets[0]


def download_wheel(asset, directory):
    digest = asset.get("digest", "")
    if not digest or not digest.startswith("sha256:"):
        raise RuntimeError("Release asset has no SHA-256 digest.")
    wheel = directory / asset["name"]
    progress(f"Downloading {wheel.name}")
    checksum = hashlib.sha256()
    received = 0
    with request(asset["browser_download_url"], "application/octet-stream") as response, wheel.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
            checksum.update(chunk)
            received += len(chunk)
            progress(f"Downloaded {received:,} / {asset['size']:,} bytes")
    if received != asset["size"] or f"sha256:{checksum.hexdigest()}" != digest:
        raise RuntimeError("Downloaded wheel size or SHA-256 does not match the Release asset. Nothing was installed.")
    return wheel


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="Brown-LEMS/B-CamSync", help="Public GitHub OWNER/REPO")
    parser.add_argument("--release", default="rolling", help="Release tag (default: rolling; example: v0.2.0)")
    parser.add_argument("--download-only", type=Path, metavar="DIRECTORY",
                        help="Save the matching wheel without installing it")
    args = parser.parse_args(argv)
    suffix = wheel_target()
    progress(f"Finding public release {args.repo}@{args.release}")
    release, asset = release_wheel(args.repo, args.release, suffix)
    progress(f"Release: {release['html_url']}\nCommit: {release['target_commitish']}")
    with tempfile.TemporaryDirectory(prefix="bcamsync-wheel-") as temporary:
        wheel = download_wheel(asset, Path(temporary))
        if args.download_only is not None:
            args.download_only.mkdir(parents=True, exist_ok=True)
            destination = args.download_only / wheel.name
            shutil.copyfile(wheel, destination)
            progress(f"Saved {destination}")
        else:
            progress(f"Installing {wheel.name} into {sys.prefix}")
            # Rolling builds can share a package version; always replace the installed build.
            subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps",
                            "--force-reinstall", str(wheel)], check=True)
            subprocess.run([sys.executable, "-c",
                            "from bcamsync import SyncConfig, SyncSession; "
                            "from bcamsync.adapters.libcamera import CameraTiming; "
                            "print('Installed core imports OK')"], check=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, URLError, OSError, subprocess.CalledProcessError) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        sys.exit(1)
