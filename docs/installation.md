# Installation

[User guide](../README.md) · [Pi 5 first run](getting-started.md) · [Integration guide](integration.md)

This library includes a native synchronization runtime and Python bindings. The recommended installation exposes `SyncSession` for application-owned capture, with the managed Arducam backend disabled. Your application supplies its camera SDK and image pipeline. The Pi 5 example uses Python libcamera for capture and OpenCV for optional display and recording.

Run commands from the root of the shared repository checkout. Use the same revision of this library and its examples. Installation builds the native extension on the Pi; allow the wheel build to finish before continuing. Examples remain source files you can copy into your own project.

## Python

The Pi 5 workflow requires Raspberry Pi OS Bookworm arm64 and Python 3.11 or later. First install and verify the B0267 vendor camera stack, including its compatible system Python libcamera bindings. This library's installer builds synchronization software; board drivers, firmware, and libcamera runtime/bindings are supplied by that camera stack. The example uses `SensorConfiguration`, `ExposureTimeMode`, and `AnalogueGainMode`; a stack with older Python bindings that lack these APIs cannot run it.

Install the build tools and OpenCV. With the working camera stack already installed, run:

```sh
sudo apt update
sudo apt install build-essential cmake pkg-config nlohmann-json3-dev python3-dev python3-venv libsystemd-dev python3-opencv
python3 -m venv --system-site-packages .venv
.venv/bin/python -c 'import cv2, libcamera; print(libcamera.SensorConfiguration, libcamera.controls.ExposureTimeMode, libcamera.controls.AnalogueGainMode)'
```

Use a new venv with system site packages so the OS camera and OpenCV modules are visible. If the last command cannot import `libcamera` or a listed API, install the Python bindings matching your vendor camera stack before proceeding; `python3-libcamera` is the OS package name when your stack uses packaged bindings. Installing this library does not update those bindings.

Install this library's synchronization interfaces and keep NumPy below 2 for Bookworm's OpenCV binary:

```sh
.venv/bin/python -m pip install . 'numpy<2' \
  --config-settings=cmake.define.BCAMSYNC_WITH_LIBCAMERA=OFF
.venv/bin/python -c 'from bcamsync import SyncConfig, SyncSession, SyncUnit; import cv2, libcamera; print("SyncSession application imports OK")'
```

`BCAMSYNC_WITH_LIBCAMERA=OFF` disables only this library's managed camera backend. The OpenCV application still uses your installed Python libcamera stack. This build needs no libcamera development headers. Linux builds still link libsystemd; the single-node `SyncSession` workflow does not need NetworkManager or chrony. Continue with [board identification and capture](getting-started.md#2-identify-the-boards).

## Optional managed capture

Choose this installation only when you want `Session` to configure cameras and deliver frame packets. The managed Arducam backend targets B0267 boards on Pi 4 and Pi 5 and needs the working vendor stack plus matching libcamera development headers, version 0.5.2 or later. Verify that development files match the runtime:

```sh
pkg-config --atleast-version=0.5.2 libcamera
pkg-config --modversion libcamera
```

After installing those development files from your camera stack, enable managed capture in the same environment:

```sh
.venv/bin/python -m pip install . 'numpy<2' \
  --config-settings=cmake.define.BCAMSYNC_WITH_LIBCAMERA=ON
```

For browser preview and recording, install Pillow alongside this backend:

```sh
.venv/bin/python -m pip install '.[preview]' 'numpy<2' \
  --config-settings=cmake.define.BCAMSYNC_WITH_LIBCAMERA=ON
```

Use [`one_node_two_boards_pi5.json`](../examples/configs/one_node_two_boards_pi5.json) for managed Pi 5 capture and fill in its camera IDs. The `arducam_b0267_pi5` profiles select the managed backend's Pi 5 behavior. Follow [managed integration](integration.md#optional-managed-capture-session) or [browser recording](deployment.md#one-pi-5-two-boards-remote-preview-and-mjpg-recording).

## Physical multi-node sessions

Both `SyncSession` and `Session` use network transport and private clock estimation for physical multi-node groups. Those deployments additionally require NetworkManager for automatic link setup and chrony for private clock estimation:

```sh
sudo apt install network-manager chrony
```

See [deployment](deployment.md) for configured membership, address policies, and optional foreground services. The Pi 5 dual-board workflow stays on one node and uses its local clock.

## C++

With the build dependencies above and a C++17 compiler, install the synchronization interfaces:

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DBCAMSYNC_BUILD_PYTHON=OFF -DBCAMSYNC_WITH_LIBCAMERA=OFF \
  -DCMAKE_INSTALL_PREFIX="$HOME/.local"
cmake --build build --parallel
cmake --install build
```

Applications use `find_package(BCamSync CONFIG REQUIRED)` and link `BCamSync::bcamsync`; pass the installation prefix through `CMAKE_PREFIX_PATH`. To use the managed [CMake consumer example](../examples/cmake_consumer), build with `-DBCAMSYNC_WITH_LIBCAMERA=ON` and provide the matching libcamera development files. Public synchronization interfaces are in [`sync.hpp`](../include/bcamsync/sync.hpp) and [`sync_unit.hpp`](../include/bcamsync/sync_unit.hpp).
