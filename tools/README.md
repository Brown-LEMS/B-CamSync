# Developer tools

Minimal Pi 5 preview examples live in [`examples/`](../examples/README.md). This directory retains the larger capture, recording, remote-preview, and management applications for development.

## CI workflows

| Workflow | Trigger | Jobs | Scope |
| --- | --- | --- | --- |
| `native.yml` | Push and pull request | 1 ARM64 job | GCC software checks for the core, managed fake capture, Python interfaces, and Release tools |
| `full-validation.yml` | Manual | 2 ARM64 jobs | Full GCC core/Release/SDK/package checks and Clang sanitizer/fuzz/static-analysis checks |
| `wheels.yml` | Manual | 5 ARM64 builds + 1 publication job | Build and install-test CPython 3.11–3.15 core wheels, then publish a public Release |

Maintainers can run full validation or publish wheels using an account with permission to dispatch workflows:

```sh
gh workflow run full-validation.yml --repo Brown-LEMS/B-CamSync --ref main
# Replace the fixed rolling prerelease with the selected commit's wheels.
gh workflow run wheels.yml --repo Brown-LEMS/B-CamSync --ref main
# Publish a persistent version and give the wheel metadata the same version.
gh workflow run wheels.yml --repo Brown-LEMS/B-CamSync --ref main -f version=0.2.0
```

The optional version accepts `X.Y.Z` or `vX.Y.Z`. Empty input updates the same `rolling` Release and moves its tag to the selected workflow commit; it is marked as a prerelease. A supplied version creates a separate `vX.Y.Z` Release, with matching Python package metadata, and leaves rolling unchanged. Existing versioned Releases are rejected rather than overwritten, and an existing version tag must already point to the selected commit. Versioned Releases persist independently of later rolling builds. Rolling retains the source package version, so its installer always reinstalls even when that version is unchanged.

The five build jobs must all succeed before [`publish_wheels.py`](publish_wheels.py) runs. It verifies the complete wheel set and metadata, uploads through a draft, and makes the Release public only after its assets, tag, and commit information agree. Rolling updates reuse the Release ID and temporarily hide it while replacing assets. Publication is serialized across workflow runs. An interrupted upload leaves a draft; inspect it before retrying. An existing rolling draft can be replaced by a later build; remove a failed versioned draft before retrying that version. GitHub Release immutability must be disabled for rolling updates; the publisher rejects an immutable rolling Release.

The workflows are independent. Publishing wheels does not trigger full validation. Daily checks do not advance the installable build. Authentication for publication uses the workflow's automatic `GITHUB_TOKEN` with write permission only in the publication job; Pi users download anonymously.

## Install a Release wheel

[`install_ci_wheel.py`](install_ci_wheel.py) uses only the Python standard library and pip to install from the public `rolling` Release. It requires no GitHub CLI, account, or token:

```sh
.venv/bin/python tools/install_ci_wheel.py
.venv/bin/python tools/install_ci_wheel.py --release v0.2.0
.venv/bin/python tools/install_ci_wheel.py --download-only dist/ci
```

It matches standard CPython 3.11–3.15 on Linux aarch64, requires glibc 2.36 or later, prints the selected Release and commit, checks asset size and SHA-256, and reinstalls only that core wheel into the running venv. It installs no camera SDK, OpenCV, or managed capture add-on. Missing, incompatible, or corrupt assets end the command. The default repository is `Brown-LEMS/B-CamSync`; `--repo OWNER/REPO` selects another public repository. See [installation](../docs/installation.md#install-a-prebuilt-core-wheel-recommended) for Pi setup and runtime dependencies.

The manual Actions wheel matrix uses [`wheels.Dockerfile`](wheels.Dockerfile) and [`build_wheel.sh`](build_wheel.sh), separately from the full GCC/Clang validation image. Each wheel is installed into a clean environment and exercises the core boundary, adapter behavior, and Release-tool tests before upload. Reproduce an ARM64 build locally with:

```sh
docker build --platform linux/arm64 -f tools/wheels.Dockerfile --build-arg PYTHON_VERSION=3.11 -t bcamsync-wheel .
mkdir -p build-linux/wheels
docker run --rm --platform linux/arm64 \
  --mount "type=bind,source=$PWD,target=/src,readonly" \
  --mount "type=bind,source=$PWD/build-linux/wheels,target=/wheels" bcamsync-wheel
.venv/bin/python -m unittest discover -s tools -p 'test_*wheel*.py' -v
```

## Capture and recording

[`opencv_capture.py`](opencv_capture.py) is the previous application-owned libcamera pipeline with synchronization reports, restart checks, browser preview, and optional MJPG AVI recording. It uses the installed synchronization core, Python libcamera, OpenCV, and NumPy. Copy its configuration template and replace both `camera_id` placeholders with the IDs reported by libcamera:

```sh
cp docs/configs/external_one_node_pi5.json group.json
.venv/bin/python -c 'import libcamera; manager = libcamera.CameraManager.singleton(); print([camera.id for camera in manager.cameras])'
```

Run synchronization checks, then record into a new directory to preserve previous files:

```sh
mkdir -p results
.venv/bin/python tools/opencv_capture.py group.json --seconds 60 --repeat 2 --output results/sync.json
.venv/bin/python tools/opencv_capture.py group.json --seconds 5 --repeat 1 \
  --video-dir results/mjpg-01 --output results/recording-sync.json
```

The recording run writes `board-a1.avi` and `board-a2.avi`, each a 1280×200 MJPG strip with four views. Check `passed` in the JSON report and decode both videos before relying on the result. Recording starts after readiness or a terminal synchronization failure; failure leaves the report unsuccessful and capture/recording continue for the requested duration. This is application behavior, separate from this library's synchronization core.

[`managed/capture.py`](managed/capture.py), [`managed/preview.py`](managed/preview.py), and [`managed/preview_record.py`](managed/preview_record.py) use the optional managed capture add-on. Keep the two browser scripts together. [`manage_group.py`](manage_group.py) uses the separately installed host client. Their deployment commands and managed recording format are described in [deployment](../docs/deployment.md).

## Remote desktop preview

Run `remote_preview.py` on your development workstation. It uses Python 3.11 or later, OpenSSH, and FFmpeg's `ffplay`; it imports no B-CamSync package, camera SDK, OpenCV, or NumPy. The remote Pi must already have this repository, its working camera environment, and a completed camera configuration. SSH must work noninteractively using your normal host configuration, keys, and known-host entry.

Start the existing external-capture application remotely and open one local window per board:

```sh
.venv/bin/python tools/remote_preview.py pi5 \
  --remote-dir /home/bapd8/B-CamSync --config group.json
```

Replace `pi5` with your SSH alias or `user@host`, and use the absolute checkout path on the Pi. `--config` is a remote path, relative to that checkout by default. `--remote-python` defaults to the checkout's `.venv/bin/python`. The tool runs `tools/opencv_capture.py --http-preview --continuous --repeat 1`, forwards the Pi's loopback HTTP port through SSH, and feeds JPEG images to local ffplay windows. Each board image contains its four camera views. It starts no recording and installs or copies no remote files.

To view a preview application that is already running:

```sh
.venv/bin/python tools/remote_preview.py pi5 --attach --remote-port 8765
```

Close any player window, press Q/Esc in a player, or press Ctrl+C in the terminal to close the tool. Launch mode sends Ctrl+C to the remote application it started so the application can restore synchronization controls and release cameras. Attach mode only closes the local players and tunnel; the existing remote application keeps running. An interrupted SSH connection can prevent confirmation of remote cleanup; the tool reports that explicitly.

The local forwarding port is selected automatically and binds only to `127.0.0.1`. `--local-port` selects a fixed port, and `--remote-port` defaults to 8765. Preview defaults to 10 fps independently of acquisition; use `--fps 5` to reduce preview traffic and `--width 960` to resize local windows. Launch mode also sets the remote preview encoder rate; attach mode only changes the local polling rate. Remote progress and synchronization status are printed in the terminal. The tool waits up to `--startup-timeout 60` seconds for startup; transport failures and stale previews end the tool rather than displaying an indefinitely frozen image.

Run its standalone checks with the project environment:

```sh
.venv/bin/python -m unittest discover -s tools -p test_remote_preview.py -v
```

These checks use a local HTTP server and substitute SSH/player executables to exercise process ownership and data flow without opening cameras or windows.
