# B-CamSync

B-CamSync is a camera synchronization library with a Rust core and interfaces for C++ and Python. This library measures frame-phase differences, applies bounded frame-period corrections through camera adapters, and reports synchronization quality within one node or across multiple nodes.

**Use `SyncSession` to add synchronization to your existing capture pipeline.** Your application configures cameras, acquires frames, owns image buffers, displays images, and records video. This library consumes sensor timing and requests corrections through device adapters. The separately installed C++ managed capture add-on can take over camera operations when that is useful to your application.

## Start on a Pi 5

New to Raspberry Pi? Follow [From a blank Raspberry Pi to a B0267 demo](docs/pi-from-scratch.md) for Raspberry Pi Imager setup with an SSH username and password, camera-stack installation, the B-CamSync package, and browser preview.

The [`examples/`](examples/README.md) directory contains six independent Python applications for Arducam B0267 boards, covering one or two boards on one Pi, or four boards across two Pi 5 nodes, with OpenCV windows or HTTP web preview. They use libcamera for capture. Complete the [Python installation](docs/installation.md#python); the scripts enumerate cameras automatically when run without camera arguments.

For a fresh Pi, use the [prebuilt core wheel](docs/installation.md#install-a-prebuilt-core-wheel-recommended) to avoid installing Rust and C++ build tools. Once the application venv is ready, run `.venv/bin/python tools/install_ci_wheel.py`. The tool anonymously downloads the rolling Release wheel for your Python version on ARM64. Use `--release vX.Y.Z` for a persistent version. The camera stack and OpenCV remain application dependencies.

One Pi 5 and one board need no inter-board synchronization. Start with the plain capture preview:

```sh
.venv/bin/python examples/single_board.py
```

With two boards, run the `SyncSession` integration:

```sh
.venv/bin/python examples/opencv_sync.py
```

For HTTP preview without a Pi display, use either corresponding script and open `http://PI_IP:8000/` in your browser:

```sh
.venv/bin/python examples/single_board_http.py
# Or, with two boards:
.venv/bin/python examples/opencv_sync_http.py
```

The single-Pi scripts select the first one or two camera IDs in sorted order and print their selection; the first board is the synchronization reference. Use `--list` to inspect IDs without starting capture, or pass IDs explicitly to choose devices and reference order. Each board appears as a grayscale strip containing its four views. The synchronization applications report quality. Use q/Esc or Ctrl+C for OpenCV preview; use Ctrl+C in the Pi terminal for HTTP preview. Closing a browser tab leaves capture running. None of the scripts needs a configuration file. Copy the chosen script into your own project to adapt its pipeline. See [the first-run guide](docs/getting-started.md) for hardware requirements and status interpretation.

For workstation preview through SSH or the existing recording pipeline, use the separate [developer tools](tools/README.md). The previously tested capture/recording application is retained as [`tools/opencv_capture.py`](tools/opencv_capture.py).

For two Pi 5 nodes with two boards each, use the [four-board examples](examples/README.md#two-pi-5-nodes-with-two-boards-each). Each node receives both IPv4 addresses and its own node identity; chrony supplies private cross-node clock estimation. Each preview shows its local two boards while status covers the four-board group.

## Integrate into your project

| Need | Interface | Guide |
| --- | --- | --- |
| Synchronize an existing acquisition pipeline | `SyncConfig` + `SyncSession` + device adapters | [Integration](docs/integration.md), [dual-board preview](examples/opencv_sync.py) |
| Connect a libcamera pipeline | `bcamsync.adapters.libcamera.CameraTiming` | [Timing hooks](docs/integration.md#reuse-the-libcamera-timing-adapter) |
| Implement an adapter for another device | `bcamsync.adapters.SyncUnit` and timing/control types | [Adapter contract](docs/integration.md#implement-a-device-adapter) |
| Let the optional add-on configure cameras and deliver frames | `bcamsync_managed.Config` + `Session` | [Managed capture](docs/integration.md#optional-managed-capture-session) |

Application code controls session lifetime and inspects quality. Device adapters translate sensor metadata and library-generated corrections into camera SDK calls. Phase estimation, correction decisions, synchronization transport, and clock mapping are internal to this library; applications do not schedule pulses themselves.

Nodes may start independently and wait for configured peers. Calling `stop()` on any participating node ends group synchronization, restores baseline controls, and warns while application capture continues. A new synchronization round requires explicit startup. The single-Pi examples use the Pi's shared local clock and need neither NetworkManager nor chrony.

## Guides

- [Fresh Pi setup](docs/pi-from-scratch.md): write Bookworm with Imager, connect over SSH, install the B0267 stack and B-CamSync, and run a demo.
- [First run on Pi 5](docs/getting-started.md): single-board preview, dual-board synchronization, and status.
- [Installation](docs/installation.md): synchronization core, optional managed capture, and C++ linking.
- [Integration](docs/integration.md): API responsibilities, adapter hooks, and lifecycle.
- [Deployment tools](docs/deployment.md): recording, browser preview, and multi-node operation.

Default readiness targets are p95 ≤ 0.5 ms and max ≤ 1 ms over two settled windows. These are sensor-timestamp acceptance thresholds; optical alignment and sustained recording throughput require separate measurements. [Design notes](docs/design.md), [device validation](tools/validate_device.py), and [build tools](tools/build.sh) cover development and validation. Software checks do not establish hardware performance.

## Contributors

Tangtangfang Fang (tangtangfang_fang@brown.edu)
