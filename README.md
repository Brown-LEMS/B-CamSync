# B-CamSync

B-CamSync is a C++ library with Python bindings for continuous camera synchronization within and across nodes. It combines network clock mapping with frame-phase control. Applications can consume frames through the library or attach synchronization to an existing capture system.

The managed backend targets Arducam B0267 four-camera boards on Raspberry Pi 4 and Pi 5 running Raspberry Pi OS Bookworm arm64. Frames are consumed locally on each node.

## Quick start

This example uses synthetic cameras and requires no hardware:

```python
from bcamsync import Config, Session

config = Config.from_file("examples/configs/fake_one_node.json")
with Session(config, "node-a") as cameras:
    while True:
        packet = cameras.read("board-a1", timeout_seconds=1)
        if packet is not None:
            print(packet.sequence, [view.shape for view in packet.views])
```

`Session` manages acquisition and synchronization; each packet contains four readonly NumPy views. Synchronization runs independently of frame consumption. Use `status()` to inspect synchronization quality and `wait_ready()` to wait for readiness.

For real cameras, start from a [configuration example](examples/configs), fill in the camera IDs and network interfaces, and run a session on each node. See [capture.py](examples/capture.py) or [capture.cpp](examples/capture.cpp) for complete capture examples.

If your application already owns capture, use `SyncSession` with `SyncUnit` timing/control adapters. Your image pipeline and camera lifetime remain under your control. See [external_capture.py](examples/external_capture.py) for a complete example.

Optional [HTTP control](examples/serve_node.py) and [browser preview](examples/preview.py) are separate node applications. Architecture and lifecycle details are in [docs/design.md](docs/design.md).

## Install

Build from the repository root on each target node. Managed Arducam capture requires the working vendor camera stack and its matching libcamera development headers (0.5.2 or later).

### Python

Requires Python 3.11 or later. On Bookworm:

```sh
sudo apt install build-essential cmake pkg-config nlohmann-json3-dev python3-dev python3-venv libsystemd-dev network-manager chrony
python3 -m venv .venv
.venv/bin/python -m pip install .
```

The package builds and includes the native library. For synthetic cameras or an existing capture system using its own SDK, install without the Arducam backend:

```sh
.venv/bin/python -m pip install . --config-settings=cmake.define.BCAMSYNC_WITH_LIBCAMERA=OFF
```

### C++

Requires C++17 and the native dependencies above:

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DBCAMSYNC_BUILD_PYTHON=OFF -DCMAKE_INSTALL_PREFIX="$HOME/.local"
cmake --build build --parallel
cmake --install build
```

Add `-DBCAMSYNC_WITH_LIBCAMERA=OFF` for SDK-independent builds. Applications use `find_package(BCamSync CONFIG REQUIRED)` and link `BCamSync::bcamsync`; pass the installation prefix through `CMAKE_PREFIX_PATH`. See the [CMake example](examples/cmake_consumer).
