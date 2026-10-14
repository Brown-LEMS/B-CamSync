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

## One Pi 5, two boards: remote preview and MJPG recording

The [preview_record.py](examples/preview_record.py) application combines browser preview with Pi-local MJPEG recording. Recording is downstream example code; the library provides frame access and synchronization. Each board writes one full-resolution 5120×800 grayscale JPEG stream containing its four views side by side, giving eight camera views across the two boards. Preview is resized and sampled independently; recording processes every packet read by the application.

Install the package with its preview dependency on the Pi using `.venv/bin/python -m pip install '.[preview]'`. Copy the Pi 5 template and enumerate the two board IDs:

```sh
cp examples/configs/one_node_two_boards_pi5.json group.json
.venv/bin/python -c 'from bcamsync import enumerate_arducam_cameras; print(enumerate_arducam_cameras("arducam_b0267_pi5"))'
```

Replace both `camera_id` placeholders in `group.json`; both units explicitly select `arducam_b0267_pi5`. Start the application with the Pi's existing IPv4 address and a writable recording directory on the Pi, replacing the example address and path:

```sh
.venv/bin/python examples/preview_record.py group.json node-a \
  --address 192.168.1.50 --output-dir /mnt/ssd/bcamsync
```

Open `http://192.168.1.50:58083` on your workstation. Click **Start node** for both boards' preview, then **Start recording**. **Stop recording** closes the files while preview continues; **Stop node** stops capture and closes any recording. Closing the browser leaves capture and recording running. The following commands provide the same control from the workstation without installing the host package:

```sh
curl --fail-with-body -X POST http://192.168.1.50:58083/start
curl --fail-with-body -X POST http://192.168.1.50:58083/record/start
curl --fail-with-body http://192.168.1.50:58083/status
# After the desired recording interval:
curl --fail-with-body -X POST http://192.168.1.50:58083/record/stop
curl --fail-with-body -X POST http://192.168.1.50:58083/stop
```

Each recording creates a new UUID directory containing `manifest.json`, two `board-N.mjpg` streams, two `board-N.jsonl` metadata files, and a final `summary.json`. The manifest maps filenames to board IDs. Each metadata line records the JPEG byte offset and length, sequence, sensor and mapped timestamps, clock validity, cumulative delivery drops, reported exposure/gain, gain lock, and view geometry. JPEGs are concatenated directly without a video container; playback timing comes from the metadata. Recording quality defaults to 90 and can be changed with `--recording-quality`; `--preview-fps` controls only preview. Progress is printed every five seconds, and `/status` includes recording counts, sequence gaps, output directory, and faults. A recording error is reported and terminates the application.

Recording starts immediately when requested and includes frames during synchronization and gain warmup. Inspect `/status` for `ready`, phase metrics, and faults, and use frame metadata to select usable intervals. The two streams are independent board packets; match their sensor timestamps for this single-node case. The dual-board fake-backend checks validate remote controls, JPEG output, metadata, recording restart, and shutdown. Sustained 40 fps recording, disk throughput, phase quality, and optical alignment still require measurements on the actual Pi 5 and boards; use [validate_device.py](tools/validate_device.py) for the separate device workflow.

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

### Pi 5 external capture test with OpenCV

[opencv_sync.py](examples/opencv_sync.py) tests two boards through `SyncSession` with application-owned libcamera requests. It supplies sensor timestamps and frame-period corrections directly, using fixed 10000 µs exposure and 1× gain. This works without a four-camera brightness calibration, including the one-camera-per-board test setup. OpenCV windows are optional; the default run measures synchronization without displaying or recording images.

Use the Pi 5 dual-board configuration above with both camera IDs filled in. The example requires the OS-provided Python libcamera and OpenCV packages, exposed through a project venv with system site packages. Keep NumPy below 2 for Bookworm's OpenCV binary:

```sh
sudo apt install python3-libcamera python3-opencv
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install . 'numpy<2'
mkdir -p results
.venv/bin/python examples/opencv_sync.py group.json \
  --seconds 60 --repeat 2 --output results/sync.json
```

Each run waits for readiness, prints phase/p95/max metrics and observed frame rates once per second, then stops synchronization and checks that external capture continues. The next run restarts synchronization on those same cameras. The JSON output retains the configuration, readiness policy, status samples, and restart results. Default readiness targets are p95 ≤ 0.5 ms and max ≤ 1 ms over two settled windows; these are acceptance thresholds, not measured results. Add `--display` only when local OpenCV windows are wanted.

For a short downstream recording, add `--seconds 5 --repeat 1 --video-dir results/mjpg`. After readiness, the example writes one 1280×200 MJPG AVI per board with all four view positions preserved horizontally. Recording remains application code in this example.

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

## Contributors
Tangtangfang Fang (tangtangfang_fang@brown.edu)