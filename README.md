# B-CamSync

B-CamSync is a C++ library with Python bindings for continuous camera synchronization within and across nodes. Applications can let the library manage cameras and deliver frames, or attach synchronization to an existing capture system.

The managed backend targets Arducam B0267 four-camera boards on Raspberry Pi 4 and Pi 5 running Raspberry Pi OS Bookworm arm64. Frames are consumed locally on each node.

## Quick start: two Raspberry Pi 4 nodes

Use one B0267 board with four cameras on each Pi 4. Install B-CamSync on both nodes using the instructions below, then connect their Ethernet ports directly.

On each node, copy the configuration and enumerate its local board ID:

```sh
cp examples/configs/two_nodes_one_board.json group.json
.venv/bin/python -c 'from bcamsync import enumerate_arducam_cameras; print(enumerate_arducam_cameras())'
```

Replace both `camera_id` placeholders with the IDs returned on the respective nodes, and put the completed `group.json` on both. The example names the nodes `node-a` and `node-b`, uses `eth0`, and selects `board-a1` as the reference. Adjust the interface names if needed. Pi 4 is the default profile; automatic networking reuses a usable interface IPv4 address or creates a temporary link-local connection.

Start the follower first, then the reference in a terminal on the other Pi:

```sh
# On node-b:
.venv/bin/python examples/capture.py group.json node-b --seconds 30
# On node-a:
.venv/bin/python examples/capture.py group.json node-a --seconds 30
```

Each node acquires its local board and prints frame sequences, four-view shapes, and synchronization status. The reference ends the group after 30 seconds. The default settings are 40 fps, 10000 µs exposure, and startup gain calibration. Other layouts are in [examples/configs](examples/configs).

## Optional network infrastructure

Networking can run independently of cameras. On each node, this installed command starts the local network and continuously announces the node, including while other nodes are offline:

```sh
.venv/bin/bcamsync-network group.json node-a
```

It reads only synchronization configuration and never initializes cameras or starts clock estimation. Run it from your node application or process supervisor when persistent networking is useful. SIGINT/SIGTERM releases only its owned network resources. Later camera sessions using automatic networking reuse the established IP; stopping capture leaves this network process running.

For an HTTP control endpoint with independently owned networking, use:

```sh
.venv/bin/bcamsync-node group.json node-a --network-startup automatic
```

HTTP discovery and configuration/status queries are available before peers or cameras start. An explicit `start` request waits for group discovery and starts capture and synchronization; `stop` keeps the network and HTTP endpoint running. A peer-discovery timeout leaves networking available for a later request. Status includes the network address and any background discovery fault.

The node command defaults to `--network-startup explicit`. In that mode it uses an already available HTTP address, supplied with `--address` or the local node's configured address; networking for synchronization starts only when capture is explicitly started. `network.mode: "auto"` versus `"existing"` continues to select address setup, independently of the command's startup policy. Applications can also use `NetworkSession.start()`, `resolve()`, and `stop()` directly, and pass a started network to `NodeService(..., network=network)`; the application retains ownership. Supplied external `SyncSession` instances use the same controller path.

## Browser preview

After the capture example finishes, connect both nodes and your workstation to the same Ethernet switch. The workstation needs an IPv4 link-local address on its Ethernet interface.

Install the preview dependency and start the example on each Pi, using its own node ID:

```sh
.venv/bin/python -m pip install '.[preview]'
.venv/bin/python examples/preview.py group.json node-a
# On the other Pi, use node-b instead.
```

On the workstation, run these commands from a checkout of this repository to install the host client. Copy `group.json` there as well:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install ./host
.venv/bin/python examples/manage_group.py group.json discover
.venv/bin/python examples/manage_group.py group.json start
```

Open the URLs returned by `discover` to see each node's four camera views. Discovery works before cameras start; the `start` command starts the whole group. To select a workstation adapter, add `--interface en7` to the host commands, replacing `en7` with your interface name. To end acquisition, run:

```sh
.venv/bin/python examples/manage_group.py group.json stop
```

Closing the browser leaves acquisition running. Preview is an independent example application that consumes frames; HTTP forwards control requests, while the node application owns capture and networking.

## Two integration methods

### Managed capture: `Session`

B-CamSync initializes cameras, manages synchronization, and delivers one board packet containing four readonly NumPy views:

```python
from bcamsync import Config, Session

with Session(Config.from_file("group.json"), "node-a") as cameras:
    while cameras.status().state != "stopped":
        packet = cameras.read("board-a1", timeout_seconds=1)
        if packet is not None:
            camera0, camera1, camera2, camera3 = packet.views
            print(packet.sequence, camera0.shape)
```

`read()` returns `None` on timeout. Synchronization runs independently of reads; use `status()` to inspect quality and `wait_ready()` when you need to wait for readiness. Exiting the session closes capture. See [capture.py](examples/capture.py) and [capture.cpp](examples/capture.cpp) for complete examples.

### Existing capture: `SyncSession`

Your application owns camera configuration, acquisition, and image delivery. Supply one `SyncUnit` adapter per local unit to expose timing observations and execute frame-period corrections:

```python
from bcamsync import SyncConfig, SyncSession

config = SyncConfig.from_file("examples/configs/external_two_nodes.json")
# capture and adapter belong to your application.
capture.start()
try:
    with SyncSession(config, "node-a", {"board-a1": adapter}) as sync:
        capture.run()
    # Synchronization is stopped; capture is still running.
finally:
    capture.stop()
```

The adapter implements `bind()`, `snapshot()`, `adjust()`, `cancel()`, and `fault()`. See [external_capture.py](examples/external_capture.py) for a complete adapter. Both integration methods share the same synchronization runtime; `SyncSession.stop()` restores controls without closing your capture system. Architecture and lifecycle details are in [docs/design.md](docs/design.md).

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
