# B-CamSync

B-CamSync (B = Brown) synchronizes cameras within and across computers, with C++ and Python interfaces. It continuously observes frame timing and adjusts frame phase against a selected reference unit. Applications can let B-CamSync manage their cameras or attach synchronization to an existing capture system.

The managed camera backend targets Arducam B0267 four-camera boards on Raspberry Pi 4 and Pi 5 running Raspberry Pi OS Bookworm arm64. Each board is one synchronization unit. A node can host several units, and a group can span several nodes. The reference unit's node coordinates synchronization; images are consumed locally on each node. Your application owns image processing, recording, and storage.

## Choose how to integrate

| Your application | Use | Who owns capture? |
| --- | --- | --- |
| Needs camera initialization and local frames | [`Session`](#capture-through-b-camsync) with `Config` | B-CamSync |
| Already operates cameras and consumes images | [`SyncSession`](#synchronize-an-existing-capture-system) with `SyncConfig` and `SyncUnit` adapters | Your application |

Both interfaces use the same synchronization runtime, clock mapping, control policy, and status reporting. HTTP control and browser preview are optional; ordinary node applications need neither a developer workstation nor a management service.

## Install

Build from this repository on each target node. Run the commands below from the repository root. For managed Arducam capture, keep the working vendor camera stack and install its matching development headers and `libcamera.pc` (libcamera 0.5.2 or later).

### Python

On Bookworm, install the build and node dependencies and create a virtual environment:

```sh
sudo apt install build-essential cmake pkg-config nlohmann-json3-dev python3-dev python3-venv libsystemd-dev network-manager chrony
python3 -m venv .venv
```

For managed Arducam capture, check the SDK and install the package:

```sh
pkg-config --modversion libcamera
.venv/bin/python -m pip install .
```

The Python package builds and includes the native C++ library. It requires Python 3.11 or later and installs NumPy and the Python build dependencies automatically.

For an existing capture system using its own SDK, or for fake-camera testing, omit the Arducam backend:

```sh
.venv/bin/python -m pip install . --config-settings=cmake.define.BCAMSYNC_WITH_LIBCAMERA=OFF
```

This still installs `Session` with fake acquisition, `SyncSession`, networking, and clock synchronization. Multi-node Ethernet setup uses NetworkManager; network clock mapping uses chrony.

### C++

The native build requires C++17, CMake, pkg-config, nlohmann-json 3.10 or later, and Linux libsystemd development headers, plus the matching libcamera SDK for managed Arducam capture.

```sh
cmake -S . -B build-release \
  -DCMAKE_BUILD_TYPE=Release \
  -DBCAMSYNC_BUILD_PYTHON=OFF \
  -DCMAKE_INSTALL_PREFIX="$HOME/.local"
cmake --build build-release --parallel
cmake --install build-release
```

Add `-DBCAMSYNC_WITH_LIBCAMERA=OFF` when using your own acquisition SDK or fake cameras. In your application's CMake project:

```cmake
find_package(BCamSync CONFIG REQUIRED)
target_link_libraries(your_application PRIVATE BCamSync::bcamsync)
```

Pass the installation prefix through `CMAKE_PREFIX_PATH`. Include `<bcamsync/session.hpp>` for managed capture or `<bcamsync/sync.hpp>` for external capture. See the [C++ capture example](examples/capture.cpp) and [standalone CMake consumer](examples/cmake_consumer).

## Configure your group

Copy a configuration from [examples/configs](examples/configs):

| Layout | Starting configuration |
| --- | --- |
| 8 cameras, 2 nodes, 1 board per node | [two_nodes_one_board.json](examples/configs/two_nodes_one_board.json) |
| 8 cameras, 1 node, 2 boards | [one_node_two_boards.json](examples/configs/one_node_two_boards.json) |
| 16 cameras, 2 nodes, 2 boards per node | [two_nodes_two_boards.json](examples/configs/two_nodes_two_boards.json) |
| Pi 5, 2 nodes, 1 board per node | [two_nodes_one_board_pi5.json](examples/configs/two_nodes_one_board_pi5.json) |
| Existing capture system, 2 nodes | [external_two_nodes.json](examples/configs/external_two_nodes.json) |

The configuration names each node and synchronization unit, assigns units to nodes, and selects `reference_unit`. Keep group identity, membership, reference, `fps`, `clock_mode`, and `phase_policy` consistent across nodes. Camera IDs, backend profiles, exposure, gain, and buffering settings describe acquisition. External `SyncConfig` files contain no camera settings; a managed `Config` can also produce a `SyncConfig` through `config.sync_config()`.

### Arducam camera settings

Enumerate the local SDK camera IDs before filling in each board's `camera_id`:

```sh
.venv/bin/python -c 'from bcamsync import enumerate_arducam_cameras; print(enumerate_arducam_cameras())'
```

For Pi 5, call `enumerate_arducam_cameras(profile="arducam_b0267_pi5")` and select that profile on its units. Pi 4 uses `arducam_b0267_pi4`, the default. Camera identity comes from SDK enumeration, rather than fixed `/dev/video*` names. Both profiles produce four grayscale `uint8` views, each 1280 × 800, from one 5120 × 800 board image.

| Setting | Scope | Default / behavior |
| --- | --- | --- |
| `fps` | Group | 40; set once at the top level |
| `profile` | Board | `arducam_b0267_pi4`; use `arducam_b0267_pi5` on Pi 5 |
| `exposure_us` | Board | 10000; fixed during acquisition |
| `auto_gain` | Board | `true`; calibrate at startup, then lock sensor gain |
| `analogue_gain` | Board | 1.0; used with `auto_gain: false` |
| `queue_capacity` | Board | 4 undelivered packets |
| `pool_capacity` | Board | 12 board-image buffers |

Automatic gain starts at 1× and meters the four views, adjusting sensor analogue gain within 1×–8× and the sensor's limits. It locks after three settled half-second observations before phase calibration. Exposure stays fixed, and calibration does not alter image pixels. Exposure must leave room within the frame period for the configured phase corrections. Geometry and raw packing belong to the profile.

## Capture through B-CamSync

Run a local `Session` on every node, using that node's ID. Start followers before the reference node. The session initializes local cameras and runs synchronization in the background while your application reads packets:

```python
from bcamsync import Config, Session

config = Config.from_file("group.json")
with Session(config, "node-a") as cameras:
    while cameras.status().state != "stopped":
        packet = cameras.read("board-a1", timeout_seconds=1)
        if packet is None:
            continue
        camera0, camera1, camera2, camera3 = packet.views
        print(packet.sequence, camera0.shape, cameras.status().ready)
```

Each packet contains one board's four views. NumPy views are readonly, have board row stride, and retain their native storage after the packet or session is released. The acquisition path copies the raw board image once into a reusable pool; constructing Python views does not copy pixels.

`read()` returns `None` on timeout (an empty shared pointer in C++). The bounded FIFO drops the oldest undelivered packet when full. Retaining all pool buffers temporarily prevents new image delivery, while timing observation and synchronization continue. Use `sequence` and cumulative `dropped_before` to track delivery gaps, and size the queue and pool for your processing workload.

The packet also carries the original `sensor_timestamp_ns`, mapped `timestamp_ns`, `clock_valid`, and requested and reported exposure/gain. Single-node mapped timestamps use local boot time; multi-node timestamps use group time. When clock mapping is unavailable, `clock_valid` is false and `timestamp_ns` is zero; pixels and the sensor timestamp remain available.

For a complete timed run on all local boards, use [capture.py](examples/capture.py):

```sh
# On node-b first, then node-a with its own ID:
.venv/bin/python examples/capture.py group.json node-b --seconds 30
```

The reference application ends the group; followers finish after its stop. In your application, stop the reference session first for orderly group shutdown. Exiting a managed session closes its cameras.

## Synchronize an existing capture system

Use `SyncSession` when your project already owns camera configuration, capture, and image delivery. Provide a `SyncUnit` adapter for every local unit:

```python
from bcamsync import SyncConfig, SyncSession

config = SyncConfig.from_file("sync.json")
# capture and adapter are supplied by your application.
capture.start()
try:
    with SyncSession(config, "node-a", {"board-a1": adapter}) as sync:
        capture.run()  # Keep your existing frame consumer and processing pipeline.
    # Synchronization has stopped; capture is still running.
finally:
    capture.stop()
```

| Adapter method | Your implementation supplies |
| --- | --- |
| `bind(session_id, round_id)` | A fresh control namespace, without starting capture |
| `snapshot()` | A `TimingSnapshot` with recent frame timings and correction feedback |
| `adjust(pulse)` | Submission of a bounded frame-period correction |
| `cancel()` | A request to restore the baseline period |
| `fault()` | A capture/control error string, or an empty string |

Callbacks run on the synchronization worker and should return promptly. Observations include frames even when their images were discarded. Preserve original sensor timestamps; observation timestamps, snapshot boundaries, control expiry, and restoration boundaries use local `CLOCK_BOOTTIME` (`boot_time_ns()`). Convert other SDK clocks into this domain and report mapping uncertainty. `sync.clock().map(local_timestamp_ns)` provides group time.

Correction acceptance and completion are separate: report the correction identity and the observed return to baseline through snapshot feedback. `SyncSession.stop()` restores synchronization controls and leaves capture running. Managed and external nodes can share a group with the same synchronization settings.

The runnable [external_capture.py](examples/external_capture.py) demonstrates a synthetic application-owned producer and a complete adapter using `PhaseActuator`. It runs without a camera SDK and demonstrates capture continuing after synchronization stops.

## Networking and synchronization status

Connect nodes directly over Ethernet, or connect them through a dedicated switch. With the default `network.mode: "auto"`, configure each node's interface (for example, `eth0`). B-CamSync activates a transient NetworkManager IPv4 link-local connection and discovers the configured peers. No router, DHCP server, or handwritten node IP addresses are required. Activation uses the application's existing NetworkManager authorization.

Multi-node sessions default to `clock_mode: "network"`: each synchronization run manages a private `chronyd -x` instance and maps local timing into the reference node's group time. System time and system chrony configuration stay unchanged. Single-node synchronization uses local time without network clock setup.

On an existing network, select `"network": {"mode": "existing"}` and provide numeric IPv4 node addresses. For same-host software endpoints, explicitly select `clock_mode: "host"`; choosing a fake camera backend does not change clock policy. Default ports are UDP 58079 for synchronization discovery, TCP 58080 for synchronization traffic, and UDP 58081 for the private clock service.

Frame access is independent of synchronization readiness. Use `status()` to inspect readiness, faults, and per-unit phase/P95/maximum error. Call `wait_ready(timeout_seconds)` when your application needs to wait for synchronized operation. Clock, transport, and phase faults clear readiness while local managed capture continues; camera failures appear through frame access. Restart a stopped or faulted run explicitly with `stop()` followed by `start()`.

Default readiness requires two settled windows with P95 error at most 0.5 ms and maximum error at most 1 ms; the default clock-quality limit is 0.1 ms. These are acceptance thresholds for the controller. Sub-millisecond alignment is the synchronization target; measure optical alignment on your hardware. Both configuration types expose `phase_policy`, so a file can override selected fields such as `"phase_policy": {"startup_delta_us": 500, "tracking_delta_us": 100}`.

### Keep a network available across capture restarts

Ordinary sessions manage their own network lifetime. If your application needs connectivity while cameras are stopped, own a separate `NetworkSession`:

```python
from bcamsync import Config, NetworkSession, Session

config = Config.from_file("group.json")
with NetworkSession(config.sync_config(), "node-a") as network:
    resolved = network.resolve()
    local = Config(config)
    local.nodes = resolved.nodes
    local.network_mode = "existing"
    with Session(local, "node-a") as cameras:
        run_application(cameras)  # Your application's frame consumer.
    # The network remains available until its outer context exits.
```

For external capture, pass `resolved` directly to `SyncSession`. Network startup establishes and announces the local address without waiting for peers; `resolve()` waits for the group. Retain this network across capture restarts. Both session types accept `set_node_addresses(addresses)` while stopped. `network.request_stop()` cancels pending discovery without releasing the link; close capture/synchronization before `network.stop()`.

## Optional workstation control and preview

During development, connect the nodes and workstation to the same dedicated Ethernet switch. The workstation needs an IPv4 link-local address on that interface and can keep Wi-Fi for Internet access. Node synchronization continues independently of the workstation.

Run the [node application](examples/serve_node.py) on each node:

```sh
.venv/bin/python examples/serve_node.py group.json node-a
```

This example owns networking, lazy managed capture, and an HTTP transport. Cameras start only after a start request. The network stays available across capture stops; exiting the example closes capture before releasing it. Use `--address` to choose an existing local HTTP address, including single-node usage without a configured synchronization interface.

On the workstation, install the independent standard-library-only host package and use discovery:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install ./host
.venv/bin/python examples/manage_group.py group.json discover
.venv/bin/python examples/manage_group.py group.json check
.venv/bin/python examples/manage_group.py group.json start
.venv/bin/python examples/manage_group.py group.json status
.venv/bin/python examples/manage_group.py group.json stop
```

Select a workstation adapter with `--interface en7` when needed. Group control starts followers before the reference node. HTTP discovery uses UDP 58082; HTTP defaults to TCP 58083. The host package requires no camera SDK, native build, or SSH credentials. Existing SSH control remains available through `--ssh-targets` and [host_targets.json](examples/configs/host_targets.json).

To preview cameras, run [preview.py](examples/preview.py) instead of the plain node application:

```sh
.venv/bin/python -m pip install '.[preview]'
.venv/bin/python examples/preview.py group.json node-a
```

Discover the node URLs and start the group with the host commands above, then open each URL in a browser. The example displays local boards and publishes the latest JPEGs at 5 fps by default. Closing a browser or host client leaves acquisition running. Preview is an independent frame-consuming application; when adding it to your own pipeline, publish from your existing consumer so readers do not compete for packets.

To add HTTP control to an existing node application, supply either session type to a controller:

```python
from bcamsync.management import NodeService
from bcamsync.http_management import HttpNodeService

controller = NodeService(config, "node-a", session=session)
http = HttpNodeService(controller, local_address)
http.serve()  # Call http.request_shutdown() from your application's shutdown path.
```

The routes are `GET /configuration`, `GET /status`, `POST /start`, and `POST /stop`. POST requests accept a JSON object with an optional `operation_id` UUID; responses contain `ok` and either `result` or `error`.

Use `active=True` if the supplied session is already running. HTTP forwards requests to the controller. Closing the transport leaves the session with its caller; `controller.close()` stops that session. For `SyncSession`, this stops synchronization while your capture keeps running. The application owns shutdown: end HTTP and preview consumers, close the controller/session, then release any separately owned network. See [serve_node.py](examples/serve_node.py) for the complete composition and signal handling.

## Try and validate

Without cameras, use [fake_one_node.json](examples/configs/fake_one_node.json) or [fake_two_nodes.json](examples/configs/fake_two_nodes.json) with the capture and preview examples. These select fake acquisition and explicit same-host clock/network settings. Synthetic phase and drift exercise correction and frame ownership through the public APIs.

For a Linux preview without the camera SDK:

```sh
.venv/bin/python -m pip install '.[preview]' --config-settings=cmake.define.BCAMSYNC_WITH_LIBCAMERA=OFF
.venv/bin/python examples/preview.py examples/configs/fake_one_node.json node-a
```

Open `http://127.0.0.1:58083` and use Start node.

For device runs, start [validate_device.py](tools/validate_device.py) on each follower, then the reference, using the same duration and repeat count:

```sh
.venv/bin/python tools/validate_device.py group.json node-b \
  --seconds 600 --repeat 2 --output node-b-results.json
```

It reports frame delivery, rates, gaps, drops, synchronization metrics, retained-frame integrity, and shutdown. Timestamp/control checks and optical alignment measurements are separate parts of device validation.

## Develop in Linux

The [Dockerfile](Dockerfile) and [build script](tools/build.sh) provide the same Linux toolchain used by GitHub Actions. On Apple Silicon, the default container platform is ARM64. GCC is the default compiler; Clang provides sanitizer, fuzzing, and static-analysis checks.

```sh
bash tools/container.sh image
bash tools/container.sh build     # Incremental compilation
bash tools/container.sh software  # Software tests and installation checks
bash tools/container.sh sdk       # Compile the Arducam backend against libcamera
bash tools/container.sh package   # Source distribution, node wheel, C++ archive, host package
bash tools/container.sh address   # ASan/UBSan and native leak checks
bash tools/container.sh thread    # Native TSan checks
```

Additional actions are `release`, `fuzz`, and `analyze`; set `BCAMSYNC_TOOLCHAIN=clang` for Clang builds or `BCAMSYNC_PLATFORM=linux/amd64` for an x86-64 host. Source is mounted readonly; builds, logs, and distribution artifacts are stored under `build-linux/`. The container SDK supports compilation and software checks; real capture uses the node's deployed vendor stack.

See [docs/design.md](docs/design.md) for architecture and design decisions.
