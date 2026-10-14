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

The package builds and includes the native library. The managed Arducam backend requires its camera SDK. Set `BCAMSYNC_WITH_LIBCAMERA=OFF` for external acquisition integrations that use their own SDK, or for software-only capture; that build still provides the synchronization runtime.

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

## Attach an existing capture system

Use `SyncSession` when your application already owns acquisition. It uses the same coordination, clock mapping, phase control, and status implementation as managed `Session`. It does not initialize cameras, consume images, configure exposure or gain, or stop your capture pipeline.

Load a camera-independent `SyncConfig`, such as [external_two_nodes.json](examples/configs/external_two_nodes.json), and provide one `SyncUnit` adapter for each local unit. Managed configurations can project the same group settings through `Config.sync_config()`. The shared handshake compares topology, reference, cadence, and clock mode; it does not depend on camera backends. Managed and external nodes can participate in the same group.

```python
from bcamsync import SyncConfig, SyncSession

config = SyncConfig.from_file("examples/configs/external_two_nodes.json")
# capture is your existing application, and adapter implements SyncUnit.
capture.start()
try:
    with SyncSession(config, "node-a", {"board-a1": adapter}) as synchronization:
        capture.run()  # Keep your existing image processing or recording flow.
finally:
    capture.stop()
```

An adapter implements `bind(session_id, round_id)`, `snapshot()`, `adjust(pulse)`, `cancel()`, and `fault()`. These methods run on the synchronization worker and should return promptly; SDK commands can be submitted to the application's existing control queue. Enqueue a `Pulse` value in C++; Python callbacks receive an owned command object that may be retained. `bind` establishes a control namespace without starting capture. `snapshot` returns recent frame timings and the latest correction feedback, including observations for frames whose images the application discarded. A returned snapshot's unit ID must identify that adapter.

`FrameTiming.sensor_timestamp_ns` retains the immutable sensor time in nanoseconds. `FrameTiming.timestamp_ns`, the snapshot boundary, restoration feedback, and control expiry use the local `CLOCK_BOOTTIME` domain (`boot_time_ns()`); adapters using another SDK clock convert it and report mapping uncertainty. `synchronization.clock().map(local_timestamp_ns)` translates local time to group time and returns zero when its mapping is unavailable.

`adjust` accepts a bounded period pulse; acceptance does not acknowledge actual completion. SDK completion feedback must identify the correction and the observed baseline-restoration boundary. `cancel` requests baseline restoration, and synchronization shutdown waits for that feedback within `request_timeout_seconds`. Missing feedback is reported in status. External capture continues after synchronization stop, remote group stop, or a synchronization fault. Reusing a `SyncSession` binds a newer round; attaching another session establishes a new namespace. Both preserve the application's capture lifetime.

The executable [external capture example](examples/external_capture.py) implements an application-owned synthetic producer using `PhaseActuator` to separate duration submission from completion. Run `.venv/bin/python examples/external_capture.py` to observe correction and capture continuing after synchronization stops. In C++, include `<bcamsync/sync.hpp>`, implement `SyncUnit`, and construct `SyncSession(config, local_node, local_interfaces)` with shared adapter ownership. Python subclasses are retained while the session uses their callbacks. Pixel data never crosses this interface.

For physical multi-node capture, the default clock mode uses the same transient Ethernet setup and private chronyd as managed acquisition. `clock_mode: "host"` explicitly selects host-clock mapping for same-host software endpoints.

## Optional developer host

HTTP management is an optional application service. Local `Session` and `SyncSession` use does not require it. Connect the nodes and developer workstation to the same dedicated Ethernet switch; the workstation needs an IPv4 link-local address on that interface. Its Wi-Fi can continue providing Internet access. The workstation discovers management endpoints and does not participate in synchronization or clock reference selection.

Run the service on each node with its deployed camera configuration:

```sh
.venv/bin/python -m bcamsync.http_management group.json node-a
```

In `auto` mode the service activates the configured Ethernet interface once and retains its temporary connection across acquisition start/stop. It discovers peer addresses before creating the local acquisition session. Exiting the service releases its network connection. `existing` mode uses configured node addresses; `--address` selects an already configured local management address. Single-node configurations without network settings can use `--address` or specify a node interface. Neither connecting a client nor querying configuration starts cameras; a client disconnect leaves acquisition running.

The HTTP interface provides `GET /configuration`, `GET /status`, `POST /start`, and `POST /stop`. POST bodies are JSON objects with an optional `operation_id` UUID. Replies contain `ok` and either `result` or `error`. The node owns its acquisition session, which local application code can access through `HttpNodeService.session`. Camera settings remain in the configuration file. Management discovery uses link-scoped multicast UDP port 58082; HTTP defaults to TCP port 58083. These are separate from synchronization and clock traffic.

Install the independent host client with `.venv/bin/python -m pip install ./host`. It uses only the Python standard library and needs no camera SDK, C++ build, NumPy, node package, SSH credentials, or targets file. On macOS it reads interface addresses with `ifconfig`; on Linux it uses `ip`. The [host example](examples/manage_group.py) discovers nodes automatically, checks their shared synchronization settings, and starts followers before the reference node. Use `--interface en7` to select a workstation adapter when desired:

```sh
.venv/bin/python examples/manage_group.py group.json discover
.venv/bin/python examples/manage_group.py group.json check
.venv/bin/python examples/manage_group.py group.json start
.venv/bin/python examples/manage_group.py group.json status
.venv/bin/python examples/manage_group.py group.json stop
```

In Python, call `bcamsync_host.discover(group_id)` and construct `HttpGroupClient(config_dict, targets)`. `discover` returns node IDs and `HttpNodeTarget.base_url` values, including services whose acquisition has not started. Group control requires every configured node to be available. The existing `bcamsync.management` Unix-socket service and SSH `GroupClient` remain available; select that transport in the host example with `--ssh-targets targets.json`.

## Browser preview example

[preview.py](examples/preview.py) is an independent node application which extends the HTTP service with a browser page and JPEG snapshots. Install its optional Pillow dependency and run it instead of the plain management service:

```sh
.venv/bin/python -m pip install '.[preview]'
.venv/bin/python examples/preview.py group.json node-a
# On the workstation, discover the page addresses and start the whole group:
.venv/bin/python examples/manage_group.py group.json discover
.venv/bin/python examples/manage_group.py group.json start
```

Open each discovered URL in a browser. The page shows each local board's four grayscale views and node state, with local start/stop controls. For a multi-node group, use the host example to control the complete group. Closing a page leaves acquisition running. The example alone consumes local frames, scales and encodes JPEGs at 5 fps by default, and retains only the latest snapshot per board. Slow browser consumption does not queue images or block acquisition. Preview remains available while synchronization is converging or reports a fault, and clears when acquisition stops. To add preview to your own pipeline, publish from your existing frame consumer rather than adding a competing `read()` consumer. Encoding, browser routes, and preview threads are absent from the library.

For a single-node fake preview on a Mac, build the Linux image and software first, then publish its HTTP port:

```sh
bash tools/container.sh image
bash tools/container.sh build
docker run --rm -p 127.0.0.1:58083:58083 \
  --mount "type=bind,source=$PWD,target=/src,readonly" \
  --mount "type=bind,source=$PWD/build-linux,target=/work/build,readonly" \
  -e PYTHONPATH=/work/build/gcc-software/python -w /src bcamsync-dev:bookworm \
  bash -c 'python examples/preview.py examples/configs/fake_one_node.json node-a --address "$(hostname -i)"'
```

Open `http://127.0.0.1:58083` and use Start node. This exercises HTTP control and preview with synthetic images; device and optical checks use the actual nodes.

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
