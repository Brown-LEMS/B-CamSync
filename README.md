# B-CamSync

B-CamSync (B = Brown) manages camera acquisition and continuous frame synchronization. Applications initialize a local session and pull board frame packets through C++ or Python. Image processing, recording, and storage belong to the application.

The first backend targets Arducam B0267 four-camera boards on Raspberry Pi 4 with Raspberry Pi OS Bookworm arm64 and the matching Arducam/libcamera SDK (0.5.2 or later). One board is one synchronization unit. A node can host multiple boards; one selected board is the reference for the whole group. Its node runs the coordinator, while every other node runs a local session and executes corrections. Images stay on their own nodes.

## Install on each node

Keep the working vendor camera stack and install its matching development headers and `libcamera.pc`. B-CamSync requires C++17, CMake, pkg-config, nlohmann-json, Python 3.11 or later, pybind11 3 or later, and NumPy. Multi-node preparation additionally uses NetworkManager, chrony, iproute2, sudo, and GNU `timeout`.

On Bookworm, install the general build and node tools, then build the Python package on the node:

```sh
sudo apt install build-essential cmake pkg-config nlohmann-json3-dev python3-dev python3-venv network-manager chrony iproute2 sudo coreutils
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

Copy one of the configurations in [examples/configs](examples/configs): `two_nodes_one_board.json` for eight cameras on two nodes, `one_node_two_boards.json` for eight cameras on one node, or `two_nodes_two_boards.json` for sixteen cameras on two nodes. Use identical group configurations on all nodes.

List each node's exact SDK camera IDs before creating a session:

```sh
.venv/bin/python -c 'from bcamsync import enumerate_arducam_cameras; print(enumerate_arducam_cameras())'
```

Replace each `camera_id` placeholder, verify the interface name, and set the desired capture dimensions, frame rate, exposure, and gain. The current backend delivers four horizontal grayscale `uint8` views per board, using `R8` directly or converting little-endian `R16` with the configured `r16_shift`. If the vendor deployment requires a pipeline JSON, set `pipeline_config` to its absolute path on every local board; enumeration accepts that path too.

For direct Ethernet, the two-node examples assign `10.77.0.1` and `10.77.0.2` without a gateway or DNS. To use an existing network, set `network.managed` to `false` and supply its addresses. Changing `reference_unit` changes the coordinator and clock reference, while node addresses stay fixed.

## Prepare a multi-node group

On each node, inspect the plan and prepare it as the operator. Replace `node-a` and `APP_USER` with the local node ID and application account:

```sh
.venv/bin/python -m bcamsync.platform plan examples/configs/two_nodes_one_board.json --node node-a --app-user APP_USER
sudo .venv/bin/python -m bcamsync.platform prepare examples/configs/two_nodes_one_board.json --node node-a --app-user APP_USER
```

Preparation activates the private network profile, saves the original network and chrony configuration, and installs an argument-free clock helper permitted for the application account. Prepare all nodes before starting capture. `Session.start()` invokes this helper to finish initial clock adjustment before opening cameras, then checks clock health throughout capture. A single-node group needs no network or clock preparation.

Use `check` to inspect the configured interface and clock. Use `restore` as root to restore the saved configuration. Before switching reference units or deployment settings, restore the old configuration on every node, then prepare the new one.

## Consume local frames

Start follower applications first, then the reference-node application. Every node uses the same configuration and its own local node ID:

```python
from bcamsync import Config, Session

config = Config.from_file("examples/configs/two_nodes_one_board.json")
with Session(config, "node-a") as session:
    if not session.wait_ready(config.startup_timeout_seconds + 10):
        raise RuntimeError("Synchronization readiness timed out")
    packet = session.read("board-a1", timeout_seconds=1)
    if packet is not None:
        camera0, camera1, camera2, camera3 = packet.views
        # Readonly, strided NumPy arrays retaining the native frame storage.
```

The complete [Python example](examples/capture.py) and [C++ example](examples/capture.cpp) consume all local boards and coordinate shutdown. Run the Python example on each node with `--seconds 30`; the reference application ends the group and followers wait for its stop. In an application, stop the reference session first for orderly group shutdown. After stopping, inspect `session.status().fault` for cleanup failures.

`read()` waits for a board packet and returns `None` on timeout; C++ returns an empty shared pointer. Synchronization continues independently of reads. Each packet exposes sequence, sensor timestamp, synchronization timestamp, and cumulative dropped count. Same-node synchronization timestamps use the local boot-time domain; multi-node timestamps use aligned realtime. Views keep their pixels alive after the packet or session is destroyed.

The image queue is a bounded FIFO: when full it drops the oldest undelivered packet. If applications retain all pool storage, new image delivery is skipped while timing observation and synchronization continue. Increase `queue_capacity` or `pool_capacity` to suit consumption and retention; the default pool holds twelve board images. The acquisition path copies each raw board image once into this reusable pool; Python view creation copies no pixels. Throughput and timing should be measured with release builds on the target nodes.

`status()` reports group readiness, faults, and each unit's phase, P95, maximum error, and last measured window boundary. Startup readiness requires two settled windows with P95 at most 0.5 ms and maximum at most 1 ms; the network clock gate is 0.1 ms by default. These are controller thresholds. The synchronization target is sub-millisecond alignment. Runtime failures stop the session and surface through status and frame access; recovery is an explicit stop and new start.

## Run the device workflow

On every node, run the operator validator using the same duration and repeat count. Start the follower first, then the reference shortly afterward:

```sh
.venv/bin/python tools/validate_device.py examples/configs/two_nodes_one_board.json node-b --seconds 600 --repeat 2 --output node-b-results.json
# On the reference node:
.venv/bin/python tools/validate_device.py examples/configs/two_nodes_one_board.json node-a --seconds 600 --repeat 2 --output node-a-results.json
```

The tool reports startup progress, per-board delivery, observed frame rate, sequence gaps, drops, timing-window metrics, retained-array integrity, and shutdown results. It validates local delivery and timestamp control; perform optical alignment measurements separately. The reference closes each run and followers confirm that shutdown before restarting. Repeat with another reference unit after restoring and preparing the node configuration.

The software implementation has been validated on macOS with acquisition fixtures, ASan/UBSan, TSan, static analysis, and Python ownership checks. Compilation against the installed vendor SDK and actual device capture remain operator checks.

## Fake acquisition and CI

Set `backend` to `fake` to exercise the public session API without cameras or node provisioning. The fake backend generates board pixels and timestamp observations, applies the same bounded phase actuator, and uses the same frame pool and queues. Per-unit `fake.initial_phase_us` and `fake.drift_ppm` specify simulated phase and drift. The [single-node configuration](examples/configs/fake_one_node.json) and [two-node loopback configuration](examples/configs/fake_two_nodes.json) provide examples. Fake acquisition uses the host's clocks; multi-host clock alignment remains part of the physical deployment.

GitHub Actions defines Linux validation for software acquisition, Python packaging, separate sanitizer builds, timing/message fuzzing, and real-backend compilation against pinned libcamera headers. The fake backend and new CI checks have not been run in this implementation round. Actual SDK capture and optical measurements remain device checks.

The architecture and durable decisions are described in [docs/design.md](docs/design.md).
