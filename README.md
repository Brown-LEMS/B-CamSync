# B-CamSync

B-CamSync manages camera acquisition and continuous frame synchronization. Applications initialize a local session and pull board frame packets through C++ or Python. Image processing, recording, and storage belong to the application.

The first backend targets Arducam B0267 four-camera boards on Raspberry Pi 4 and Pi 5 with Raspberry Pi OS Bookworm arm64 and the matching Arducam/libcamera SDK (0.5.2 or later). One board is one synchronization unit. A node can host multiple boards; one selected board is the reference for the whole group. Its node runs the coordinator, while every other node runs a local session and executes corrections. Images stay on their own nodes.

## Install on each node

Keep the working vendor camera stack and install its matching development headers and `libcamera.pc`. B-CamSync requires C++17, CMake, pkg-config, nlohmann-json, Python 3.11 or later, pybind11 3 or later, and NumPy. Linux builds also require libsystemd development headers. Physical multi-node sessions use NetworkManager and chrony.

On Bookworm, install the general build and node tools, then build the Python package on the node:

```sh
sudo apt install build-essential cmake pkg-config nlohmann-json3-dev python3-dev python3-venv libsystemd-dev network-manager chrony
pkg-config --modversion libcamera
python3 -m venv .venv
.venv/bin/python -m pip install .
```

The package builds and includes the native library. A Linux build requires the camera SDK; a build with `BCAMSYNC_WITH_LIBCAMERA=OFF` is only for software validation and cannot open cameras.

For a C++ installation:

```sh
cmake -S . -B build-release -DCMAKE_BUILD_TYPE=Release -DBCAMSYNC_BUILD_PYTHON=OFF -DCMAKE_INSTALL_PREFIX="$HOME/.local"
cmake --build build-release --parallel
cmake --install build-release
```

Applications link `BCamSync::bcamsync` after `find_package(BCamSync CONFIG REQUIRED)` and include `<bcamsync/session.hpp>`. Pass the installation prefix through `CMAKE_PREFIX_PATH`.

## Configure boards and nodes

Copy one of the configurations in [examples/configs](examples/configs): `two_nodes_one_board.json` for eight cameras on two nodes, `one_node_two_boards.json` for eight cameras on one node, or `two_nodes_two_boards.json` for sixteen cameras on two nodes. Keep group membership, reference unit, backend, and group frame rate consistent on all nodes. Camera IDs, platform profiles, exposure, gain, and buffering settings belong to each node's acquisition configuration.

List each node's exact SDK camera IDs before creating a session:

```sh
.venv/bin/python -c 'from bcamsync import enumerate_arducam_cameras; print(enumerate_arducam_cameras())'
```

Replace each `camera_id` placeholder and verify the interface name. Both platform profiles use the same full `5120×800` board image, 8-bit sensor, R8 buffers, and four horizontal grayscale `uint8` views. `arducam_b0267_pi4` selects `/usr/share/libcamera/pipeline/rpi/vc4/rpi_apps.yaml`; `arducam_b0267_pi5` uses the default pipeline. Geometry and pipeline settings belong to the backend profile; configure neither width, height, nor view count for physical boards.

For Pi 5, select `"profile": "arducam_b0267_pi5"` on its units and enumerate with `enumerate_arducam_cameras(profile="arducam_b0267_pi5")`. The [Pi 5 two-node example](examples/configs/two_nodes_one_board_pi5.json) includes this selection. A group can contain Pi 4 and Pi 5 nodes; all boards on a node use that node's platform profile. Camera IDs come from the local SDK enumeration rather than fixed `/dev/video*` numbers.

Set the top-level `fps` once for the group, and set `exposure_us` and the gain policy on individual units before starting a session. Exposure and gain apply to the whole board. Defaults are 40 fps, 10000 µs exposure, and `auto_gain: true`: startup calibration begins at 1×, meters the four views, and adjusts sensor analogue gain within the intersection of sensor limits and 1×–8×. Gain locks after three settled half-second observations, before phase calibration begins; it stays fixed throughout the session. Set `auto_gain: false` and choose `analogue_gain` for manual gain. Exposure stays fixed in both modes, and calibration never modifies image pixels.

## Connect a multi-node group

The default `network.mode` is `"auto"`. Connect the nodes over a dedicated Ethernet link and set each node's synchronization `interface`, such as `eth0`. `Session.start()` starts local acquisition, activates a transient NetworkManager IPv4 link-local connection, discovers the configured node IDs on that interface, and establishes the group clock. Startup reports address assignment, discovery, and clock convergence progress. The application account uses NetworkManager's existing activation authorization.

Each physical multi-node session runs its own `chronyd -x` process and maps sensor timestamps into the reference node's group time. This process leaves the system clock and system chrony configuration unchanged. Session shutdown releases its temporary network connection and clock process. Single-node groups use local sensor time and need no network or clock service.

To use an already configured network, set `"network": {"mode": "existing"}` and supply each node's numeric IPv4 `address`. This mode retains existing addressing and uses the same private clock estimator. The default synchronization TCP port is 58080 and the private clock UDP port is 58081; `network.clock_port` selects another clock port. Changing `reference_unit` changes coordinator and clock-source roles on the next session.

## Consume local frames

Start follower applications first, then the reference-node application. Every node loads its acquisition configuration and uses its own local node ID:

```python
from bcamsync import Config, Session

config = Config.from_file("examples/configs/two_nodes_one_board.json")
with Session(config, "node-a") as session:
    packet = session.read("board-a1", timeout_seconds=30)
    if packet is not None:
        camera0, camera1, camera2, camera3 = packet.views
        # Readonly, strided NumPy arrays retaining the native frame storage.
        print(session.status().state, packet.clock_valid)
```

The complete [Python example](examples/capture.py) and [C++ example](examples/capture.cpp) consume all local boards and coordinate shutdown. Run the Python example on each node with `--seconds 30`; the reference application ends the group and followers wait for its stop. In an application, stop the reference session first for orderly group shutdown. After stopping, inspect `session.status().fault` for cleanup failures.

`read()` waits for a board packet and returns `None` on timeout; C++ returns an empty shared pointer. Synchronization continues independently of reads. Each packet exposes sequence, sensor timestamp, synchronization timestamp, `clock_valid`, cumulative dropped count, requested and reported exposure/gain, and gain-lock state. Sensor controls have pipeline delay and quantization, so reported values may differ from requests. Single-node synchronization timestamps use the local boot-time domain; multi-node timestamps use the private group clock mapping. When that mapping is unavailable, `clock_valid` is false and `timestamp_ns` is zero; the local sensor timestamp and image remain available. Views keep their pixels alive after the packet or session is destroyed.

The image queue is a bounded FIFO: when full it drops the oldest undelivered packet. If applications retain all pool storage, new image delivery is skipped while timing observation and synchronization continue. Increase `queue_capacity` or `pool_capacity` to suit consumption and retention; the default pool holds twelve board images. The acquisition path copies each raw board image once into this reusable pool; Python view creation copies no pixels. Throughput and timing should be measured with release builds on the target nodes.

`status()` reports group readiness, faults, and each unit's phase, P95, maximum error, and last measured window boundary. Startup readiness requires two settled windows with P95 at most 0.5 ms and maximum at most 1 ms; the network clock gate is 0.1 ms by default. These are controller thresholds. The synchronization target is sub-millisecond alignment. Frame access works during synchronization startup and after a synchronization fault. Clock, transport, or phase faults clear readiness and appear in status while local acquisition continues. Camera failures appear through frame access. Applications that require synchronized frames may call `wait_ready()` and inspect status; restarting synchronization requires an explicit stop and new start.

## Optional developer host

Applications can use local `Session` directly without a developer host. For remote development, `bcamsync.management` supplies a thin `NodeService` around a node-local Session. The separate `bcamsync_host` package supplies `GroupClient` and `NodeTarget` for management requests over SSH. The reference unit's node remains the synchronization coordinator. A client disconnect leaves node sessions running; stopping requires an explicit command.

Deploy each node's configuration and run the optional foreground service there:

```sh
.venv/bin/python -m bcamsync.management serve group.json node-a --socket /run/user/1000/bcamsync.sock
```

The service accepts configuration inspection, start, status, and stop through its local Unix socket. It does not start acquisition until requested. It can run under the project's existing service supervision; it does not install a daemon or change configuration files. Node applications can use `NodeService.session` to consume local frames while serving management requests in another thread.

Install the host client in a developer-host virtual environment with `.venv/bin/python -m pip install ./host`. This package uses the Python standard library and requires no camera SDK, C++ build, NumPy, or node package.

The [host example](examples/manage_group.py) reads a separate targets JSON mapping node IDs to `ssh_host`, `python`, and `socket` on each node. Copy [host_targets.json](examples/configs/host_targets.json) and adjust the SSH aliases, installed Python paths, and socket paths. It checks shared synchronization settings before starting followers and then the reference node; local camera, exposure, gain, and buffering settings may differ. Host targets describe management access and are separate from synchronization-network addresses. The host interface carries commands and status; images stay local.

```sh
.venv/bin/python examples/manage_group.py group.json targets.json check
.venv/bin/python examples/manage_group.py group.json targets.json start
.venv/bin/python examples/manage_group.py group.json targets.json status
.venv/bin/python examples/manage_group.py group.json targets.json stop
```

## Run the device workflow

On every node, run the operator validator using the same duration and repeat count. Start the follower first, then the reference shortly afterward:

```sh
.venv/bin/python tools/validate_device.py examples/configs/two_nodes_one_board.json node-b --seconds 600 --repeat 2 --output node-b-results.json
# On the reference node:
.venv/bin/python tools/validate_device.py examples/configs/two_nodes_one_board.json node-a --seconds 600 --repeat 2 --output node-a-results.json
```

The tool reports startup progress, per-board delivery, observed frame rate, sequence gaps, drops, timing-window metrics, retained-array integrity, and shutdown results. It validates local delivery and timestamp control; perform optical alignment measurements separately. The reference closes each run and followers confirm that shutdown before restarting. Repeat with another reference unit after updating the configurations between sessions.

Run device capture and optical alignment measurements on the target nodes with their installed vendor SDK.

## Fake acquisition and CI

Set `backend` to `fake` to exercise the public session API without cameras or node provisioning. The fake backend generates board pixels and timestamp observations, applies the same bounded phase actuator, and uses the same frame pool and queues. Per-unit `fake.width` and `fake.height` specify synthetic geometry; `fake.initial_phase_us` and `fake.drift_ppm` specify simulated phase and drift. The [single-node configuration](examples/configs/fake_one_node.json) and [two-node loopback configuration](examples/configs/fake_two_nodes.json) provide examples. Fake acquisition uses the host's clocks; multi-host clock alignment remains part of the physical deployment.

GitHub Actions uses the same Debian development image and build entry points as local container development. GCC checks run on native ARM64 and x86-64 runners; Clang supplies supplementary compilation, sanitizer, fuzzing, and static-analysis checks. CI retains build logs, source distributions, C++ archives, and Python wheels as artifacts. Actual capture and optical measurements are device checks.

## Develop and build in Linux

The [Dockerfile](Dockerfile) provides Debian 12, GCC 12, Clang 16, a separate Python 3.11 virtual environment, and pinned official libcamera 0.5.2 headers and libraries. Apple Silicon development uses native `linux/arm64` containers. Build the image once, then compile incrementally:

```sh
bash tools/container.sh image
bash tools/container.sh build
```

The launcher mounts the source read-only and stores generated files in `build-linux/`. GCC is the default toolchain. Local development and GitHub Actions both call [tools/build.sh](tools/build.sh) through the same launcher. On an x86-64 host, select `BCAMSYNC_PLATFORM=linux/amd64`. Run `bash tools/container.sh` without arguments to open the development shell; its active Python environment belongs to the Linux image.

| Action | Build and validation |
| --- | --- |
| `build` | Incremental Debug compilation with fake acquisition available; shares the `software` build directory |
| `software` | Debug build, native/Python checks, and independent CMake/Python installation checks |
| `release` | Release build with the real backend, software checks, and installed-package checks |
| `sdk` | Compile the real backend and Python interface against the available SDK |
| `package` | Build the node wheel from its source distribution, produce a C++ archive and separate host package, and validate isolated installations |
| `address` | Clang ASan/UBSan, native leak checking, and instrumented Python ownership checks |
| `thread` | Separate Clang TSan build for native concurrency checks |
| `fuzz` | Bounded LLVM timing/control-message fuzzing, default 60 seconds |
| `analyze` | Clang static analysis of native and SDK resource paths |

```sh
bash tools/container.sh software
bash tools/container.sh package
bash tools/container.sh address
bash tools/container.sh thread
BCAMSYNC_TOOLCHAIN=clang bash tools/container.sh sdk
```

`BCAMSYNC_TOOLCHAIN=clang` selects the supplementary compiler for normal builds; safety actions select Clang themselves. `BCAMSYNC_JOBS` controls build parallelism (default two). Logs are saved in `build-linux/logs/`; packages are saved in `build-linux/packages/gcc-aarch64/` for the default ARM64 build. Compiler and action build directories are separate. The image's SDK supports API compilation and fake acquisition checks; node installation uses the matching deployed Arducam SDK.

Build, ownership, and packaging checks are software validation. NetworkManager activation, private-clock convergence, real camera throughput, and optical alignment require measurements on the target nodes.

The architecture and durable decisions are described in [docs/design.md](docs/design.md).
