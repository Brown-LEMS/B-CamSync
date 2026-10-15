# Installation

[Back to the user guide](../README.md) · [First run on Pi 5](getting-started.md)

Run the following build commands from a checkout of the revision you intend to use, on each target node. Installing the Python package includes the native library; examples remain application source files that you can copy into your own project. Use the same revision for the package and its examples when handing a setup to another lab member.

Managed Arducam capture requires the working vendor camera stack and its matching libcamera development headers (0.5.2 or later). For the Pi 5 dual-board setup, verify that the OS can enumerate both boards before building. Keep the vendor runtime, development files, and Python libcamera bindings compatible with each other.

## Python

Requires Python 3.11 or later. Install the build dependencies on Bookworm:

```sh
sudo apt install build-essential cmake pkg-config nlohmann-json3-dev python3-dev python3-venv libsystemd-dev network-manager chrony
```

The current Linux build links libsystemd. NetworkManager and chrony are used by physical multi-node networking and clock estimation; single-node sessions do not start them. Choose the OpenCV installation below for the recommended Pi 5 application, or the managed installation for `capture.py` and browser applications.

### OpenCV application on Pi 5

The OpenCV example uses OS-provided Python camera and image modules. Create the venv with system site packages so it can import them, and keep NumPy below 2 for Bookworm's OpenCV binary. If `.venv` already exists without system site packages, create a separate environment with this option and use that environment's Python in the example commands.

```sh
sudo apt install python3-libcamera python3-opencv
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install . 'numpy<2'
.venv/bin/python -c 'import bcamsync, cv2, libcamera; print("Camera application imports OK")'
```

Continue with the [Pi 5 first-run guide](getting-started.md). Installing OS packages should preserve a compatible vendor camera stack; use the bindings supplied with that stack if its installation already provides them.

### Managed capture applications

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
```

For browser preview and recording, install the optional Pillow dependency using `.venv/bin/python -m pip install '.[preview]'`. See the [example catalog](../examples/README.md) for application files and the [deployment guide](deployment.md) for controls and outputs. If you also run the OpenCV application in this environment, keep its `numpy<2` constraint when installing extras.

### External SDK or synthetic capture

For an existing camera system using its own SDK, or the synthetic `external_capture.py` example, you can disable the managed Arducam backend:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install . --config-settings=cmake.define.BCAMSYNC_WITH_LIBCAMERA=OFF
```

This removes the managed backend's libcamera development dependency. Your application supplies its own camera SDK and a `SyncUnit` adapter; Linux's libsystemd build dependency remains. The OpenCV example still needs Python libcamera and OpenCV at runtime even with this build option.

## C++

Requires C++17 and the native dependencies above:

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DBCAMSYNC_BUILD_PYTHON=OFF -DCMAKE_INSTALL_PREFIX="$HOME/.local"
cmake --build build --parallel
cmake --install build
```

Add `-DBCAMSYNC_WITH_LIBCAMERA=OFF` for SDK-independent builds. Applications use `find_package(BCamSync CONFIG REQUIRED)` and link `BCamSync::bcamsync`; pass the installation prefix through `CMAKE_PREFIX_PATH`. See the [CMake example](../examples/cmake_consumer).
