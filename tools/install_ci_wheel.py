#!/usr/bin/env python3
"""Install the core wheel from the latest successful main push using GitHub CLI."""

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile


def progress(message):
    print(message, flush=True)


def github(*args):
    result = subprocess.run(["gh", *args], check=True, text=True, capture_output=True)
    return json.loads(result.stdout)


def wheel_target():
    """Match the ordinary CPython ABI and Bookworm-compatible Linux targets."""
    if sys.prefix == sys.base_prefix and os.environ.get("CONDA_PREFIX") != sys.prefix:
        raise RuntimeError("Run this script with a project venv or Conda interpreter.")
    if sys.implementation.name != "cpython" or sys.version_info < (3, 11):
        raise RuntimeError("Prebuilt wheels require CPython 3.11 or later.")
    if sysconfig.get_config_var("Py_GIL_DISABLED") or sysconfig.get_config_var("Py_DEBUG"):
        raise RuntimeError("Prebuilt wheels require a standard, non-debug CPython build with the GIL.")
    arch = platform.machine()
    if platform.system() != "Linux" or arch not in ("aarch64", "x86_64"):
        raise RuntimeError("Prebuilt wheels support Linux aarch64 and x86_64.")
    libc, version = platform.libc_ver()
    if libc != "glibc" or tuple(map(int, version.split("."))) < (2, 36):
        raise RuntimeError("Prebuilt wheels require glibc 2.36 or later (Debian 12 / Bookworm).")
    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
    return f"wheels-cpython{minor}-linux_{arch}", f"-{abi}-{abi}-linux_{arch}.whl"


def latest_run(repo, branch):
    runs = github("run", "list", "--repo", repo, "--workflow", "native.yml",
                  "--branch", branch, "--event", "push", "--status", "success",
                  "--limit", "1", "--json", "databaseId,headSha,url")
    if not runs:
        raise RuntimeError(f"No successful native.yml push build exists for {repo}@{branch}.")
    return runs[0]


def require_artifact(repo, run_id, name):
    page = 1
    while True:
        result = github("api", f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100&page={page}")
        for artifact in result["artifacts"]:
            if artifact["name"] == name:
                if artifact["expired"]:
                    raise RuntimeError(f"Artifact {name} has expired; a new successful build is required.")
                return
        if len(result["artifacts"]) < 100:
            break
        page += 1
    raise RuntimeError(f"Build {run_id} has no {name} artifact. Check its wheel matrix and Python support.")


def download_wheel(repo, run_id, name, suffix, directory):
    progress(f"Downloading {name}")
    subprocess.run(["gh", "run", "download", str(run_id), "--repo", repo,
                    "--name", name, "--dir", str(directory)], check=True)
    wheels = [path for path in directory.rglob("bcamsync-*.whl") if path.name.endswith(suffix)]
    if len(wheels) != 1:
        raise RuntimeError(f"Expected one compatible core wheel in {name}; found {len(wheels)}.")
    return wheels[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="Brown-LEMS/B-CamSync", help="GitHub OWNER/REPO")
    parser.add_argument("--branch", default="main", help="Branch to install (default: main)")
    parser.add_argument("--download-only", type=Path, metavar="DIRECTORY",
                        help="Save the matching wheel without installing it")
    args = parser.parse_args(argv)
    artifact, suffix = wheel_target()
    if shutil.which("gh") is None:
        raise RuntimeError("Install GitHub CLI (gh) and run 'gh auth login' before using this tool.")
    progress(f"Finding the latest successful push build for {args.repo}@{args.branch}")
    run = latest_run(args.repo, args.branch)
    progress(f"Build: {run['url']}\nCommit: {run['headSha']}")
    require_artifact(args.repo, run["databaseId"], artifact)
    with tempfile.TemporaryDirectory(prefix="bcamsync-wheel-") as temporary:
        wheel = download_wheel(args.repo, run["databaseId"], artifact, suffix, Path(temporary))
        if args.download_only is not None:
            args.download_only.mkdir(parents=True, exist_ok=True)
            destination = args.download_only / wheel.name
            shutil.copyfile(wheel, destination)
            progress(f"Saved {destination}")
        else:
            progress(f"Installing {wheel.name} into {sys.prefix}")
            # Prototype builds share a package version; replace an older build of that version.
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
    except (RuntimeError, subprocess.CalledProcessError) as error:
        detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
        print(f"Installation failed: {detail}", file=sys.stderr)
        sys.exit(1)
