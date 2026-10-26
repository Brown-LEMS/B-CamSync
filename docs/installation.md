# Installation

[User guide](../README.md) · [Pi 5 first run](getting-started.md) · [Integration guide](integration.md)

This library includes a Rust synchronization core and Python bindings. The recommended installation exposes `SyncSession` for application-owned capture. Managed C++ acquisition is a separate optional package, `bcamsync-managed`. Your application supplies its camera SDK and image pipeline. The Pi 5 examples use Python libcamera for capture and OpenCV for local preview or HTTP JPEG encoding; the HTTP examples require no Pi display. Recording and SSH desktop preview applications are separate tools.

Run commands from the root of the shared repository checkout. Use the same revision of this library and its examples. Prebuilt core wheels avoid compiling Rust and C++ on the Pi. Examples remain source files you can copy into your own project.

## Python

The Pi 5 workflow requires Raspberry Pi OS Bookworm arm64 and Python 3.11 or later. First install and verify the B0267 vendor camera stack, including its compatible system Python libcamera bindings. This library's installer builds synchronization software; board drivers, firmware, and libcamera runtime/bindings are supplied by that camera stack. The example uses `SensorConfiguration`, `ExposureTimeMode`, and `AnalogueGainMode`; a stack with older Python bindings that lack these APIs cannot run it.

### Install a prebuilt core wheel (recommended)

The manually triggered `wheels.yml` GitHub Actions workflow builds core wheels for standard CPython 3.11–3.15 on Linux arm64, using Debian 12 / Bookworm. They require glibc 2.36 or later and the `libsystemd0` runtime; no Rust compiler, C++ compiler, or development headers are required on the target. Python minor versions have separate wheels. Free-threaded and debug interpreters are not included. Camera bindings must match your chosen Python interpreter independently; on Bookworm, use the system Python 3.11 for the OS camera and OpenCV packages.

With the working vendor stack installed, prepare the application environment and authenticate GitHub CLI using an account with read access to this repository and its Actions artifacts:

```sh
sudo apt update
sudo apt install python3-venv python3-opencv libsystemd0 gh
python3 -m venv --system-site-packages .venv
gh auth login
.venv/bin/python tools/install_ci_wheel.py
.venv/bin/python -c 'from bcamsync import SyncConfig, SyncSession; from bcamsync.adapters.libcamera import CameraTiming; import cv2, libcamera; print("SyncSession and camera pipeline imports OK")'
```

The tool selects the latest successful manual `wheels.yml` build on `main`, prints its URL and commit, downloads the wheel matching the running interpreter and architecture, and installs only the synchronization core into that venv. It replaces an existing build even when the package version is unchanged. It reports a missing or expired artifact directly; it does not switch to an older build or compile from source. A maintainer must trigger the wheel workflow and wait for all five Python builds to succeed before this installation path is available. Daily push/PR checks do not publish wheels, so the latest installable wheel can be older than the latest source commit. Keep the examples checkout at the printed commit when reproducing a build.

Use `--branch BRANCH` for another branch or `--download-only DIRECTORY` to save the matching wheel without installing it. The default repository is `Brown-LEMS/B-CamSync`; `--repo OWNER/REPO` selects a fork with the same workflow. To download manually, open a successful [ARM64 core wheels run](https://github.com/Brown-LEMS/B-CamSync/actions/workflows/wheels.yml), choose `wheels-cpython3.11-linux_aarch64` for a Bookworm Pi, unzip it, and install its `bcamsync-*.whl` using `.venv/bin/python -m pip install /path/to/wheel.whl`. Actions artifacts are temporary build outputs, not a PyPI package index.

### Build from source

Install [rustup](https://rustup.rs/), then select Rust 1.99.0, the toolchain used for validation. A minimum supported Rust version has not been established. Rust is needed to build this library from source; applications using an installed wheel do not need the compiler. With the working camera stack already installed, install the C++ build tools and OpenCV:

```sh
rustup toolchain install 1.99.0
export RUSTUP_TOOLCHAIN=1.99.0
rustc --version
cargo --version
sudo apt update
sudo apt install build-essential cmake pkg-config nlohmann-json3-dev python3-dev python3-venv libsystemd-dev python3-opencv
python3 -m venv --system-site-packages .venv
.venv/bin/python -c 'import cv2, libcamera; print(libcamera.SensorConfiguration, libcamera.controls.ExposureTimeMode, libcamera.controls.AnalogueGainMode)'
```

Use a new venv with system site packages so the OS camera and OpenCV modules are visible. If the last command cannot import `libcamera` or a listed API, install the Python bindings matching your vendor camera stack before proceeding; `python3-libcamera` is the OS package name when your stack uses packaged bindings. Installing this library does not update those bindings.

Install this library's synchronization interfaces and keep NumPy below 2 for Bookworm's OpenCV binary:

```sh
.venv/bin/python -m pip install . 'numpy<2'
.venv/bin/python -c 'from bcamsync import SyncConfig, SyncSession; from bcamsync.adapters.libcamera import CameraTiming; import cv2, libcamera; print("SyncSession and CameraTiming application imports OK")'
```

The core package includes the reusable `bcamsync.adapters.libcamera.CameraTiming` metadata/control adapter, which imports no camera SDK or image dependencies. The core package does not include managed camera acquisition and has no NumPy dependency. The OpenCV application uses your installed Python libcamera stack and NumPy; keep its NumPy pin when installing application dependencies. The core build needs no libcamera development headers. Linux builds still link libsystemd; the single-node `SyncSession` workflow does not need NetworkManager or chrony. Continue with [board identification and capture](getting-started.md#2-identify-the-boards).

## Optional managed capture

Install the separate `bcamsync-managed` package only when you want `Session` to configure cameras and deliver frame packets. Install the core above first; the add-on links that installation and does not rebuild or bundle a second copy of the core. The managed Arducam backend targets B0267 boards on Pi 4 and Pi 5 and needs the working vendor stack plus matching libcamera development headers, version 0.5.2 or later. Verify that development files match the runtime:

```sh
pkg-config --atleast-version=0.5.2 libcamera
pkg-config --modversion libcamera
```

After installing those development files from your camera stack, install the add-on from its own directory in the same environment. The explicit build tools and `--no-build-isolation` let its build locate the core you just installed:

```sh
.venv/bin/python -m pip install 'scikit-build-core>=0.10' 'pybind11>=3.0'
.venv/bin/python -m pip install ./addons/managed 'numpy<2' --no-build-isolation \
  --config-settings=cmake.define.BCAMSYNC_WITH_LIBCAMERA=ON
.venv/bin/python -c 'from bcamsync_managed import Config, Session; print("Managed capture imports OK")'
```

For browser preview and recording, install Pillow alongside this backend:

```sh
.venv/bin/python -m pip install 'Pillow>=11' 'numpy<2'
```

New applications can import `Config`, `Session`, and `FramePacket` from `bcamsync_managed`. Existing explicit `from bcamsync import Config, Session` imports resolve to the installed add-on for compatibility. Importing `bcamsync` alone does not import camera or image bindings.

Use [`one_node_two_boards_pi5.json`](configs/one_node_two_boards_pi5.json) for managed Pi 5 capture and fill in its camera IDs. The `arducam_b0267_pi5` profiles select the managed backend's Pi 5 behavior. Follow [managed integration](integration.md#optional-managed-capture-session) or [browser recording](deployment.md#one-pi-5-two-boards-remote-preview-and-mjpg-recording).

## Physical multi-node sessions

Both `SyncSession` and `Session` use network transport and private clock estimation for physical multi-node groups. Those deployments additionally require NetworkManager for automatic link setup and chrony for private clock estimation:

```sh
sudo apt install network-manager chrony
```

See [deployment](deployment.md) for configured membership, address policies, and optional foreground services. The Pi 5 dual-board workflow stays on one node and uses its local clock.

## C++

With the Rust toolchain, build dependencies above, and a C++17 compiler, install the synchronization core:

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DBCAMSYNC_BUILD_PYTHON=OFF -DBCAMSYNC_BUILD_MANAGED=OFF \
  -DCMAKE_INSTALL_PREFIX="$HOME/.local"
cmake --build build --parallel
cmake --install build
```

Applications use `find_package(BCamSync CONFIG REQUIRED)` and link `BCamSync::bcamsync`; pass the installation prefix through `CMAKE_PREFIX_PATH`. For managed capture, build and install the add-on against that installed core, with matching libcamera development files:

```sh
cmake -S addons/managed -B build-managed -DCMAKE_BUILD_TYPE=Release \
  -DBCAMSYNC_BUILD_PYTHON=OFF -DBCAMSYNC_WITH_LIBCAMERA=ON \
  -DCMAKE_PREFIX_PATH="$HOME/.local" -DCMAKE_INSTALL_PREFIX="$HOME/.local"
cmake --build build-managed --parallel
cmake --install build-managed
```

Managed applications use `find_package(BCamSyncManaged CONFIG REQUIRED)` and link `BCamSync::managed`. Public synchronization interfaces are in [`sync.hpp`](../include/bcamsync/sync.hpp) and [`sync_unit.hpp`](../include/bcamsync/sync_unit.hpp). Independent CMake consumers under `tests/` check installation; the user-facing examples currently cover Python.

When updating an existing checkout, rebuild native applications against the current core and add-on libraries. Managed C++ applications link the separate `BCamSync::managed` target, and managed Python applications install both packages. The dual-board Python preview example uses only the core package. Prototype APIs may change; keep the library, tools, and examples on the same revision.
