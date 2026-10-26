# Developer tools

Minimal Pi 5 preview examples live in [`examples/`](../examples/README.md). This directory retains the larger capture, recording, remote-preview, and management applications for development.

## Install a CI wheel

[`install_ci_wheel.py`](install_ci_wheel.py) installs the core from the latest successful `native.yml` push build on `main`. Run it with your application venv after installing and authenticating GitHub CLI (`gh auth login`):

```sh
.venv/bin/python tools/install_ci_wheel.py
.venv/bin/python tools/install_ci_wheel.py --branch dev/my-branch --download-only dist/ci
```

It matches standard CPython 3.11–3.15 and Linux aarch64/x86_64, requires glibc 2.36 or later, prints the selected build commit, and reinstalls that core wheel into the running venv even if its version is unchanged. It installs no camera SDK, OpenCV, or managed capture add-on. Missing, expired, or incompatible artifacts end the command. See [installation](../docs/installation.md#install-a-prebuilt-core-wheel-recommended) for Pi setup and runtime dependencies.

The Actions wheel matrix uses [`wheels.Dockerfile`](wheels.Dockerfile) and [`build_wheel.sh`](build_wheel.sh), separately from the full GCC/Clang validation image. Each wheel is installed into a clean environment and exercises the core boundary, adapter behavior, and installer tests before upload. Reproduce an ARM64 build locally with:

```sh
docker build --platform linux/arm64 -f tools/wheels.Dockerfile --build-arg PYTHON_VERSION=3.11 -t bcamsync-wheel .
mkdir -p build-linux/wheels
docker run --rm --platform linux/arm64 \
  --mount "type=bind,source=$PWD,target=/src,readonly" \
  --mount "type=bind,source=$PWD/build-linux/wheels,target=/wheels" bcamsync-wheel
.venv/bin/python -m unittest discover -s tools -p test_install_ci_wheel.py -v
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
