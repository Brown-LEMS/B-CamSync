# B-CamSync

B-CamSync is a camera synchronization library with C++ and Python interfaces. It continuously aligns frame timing and reports synchronization quality, within one node or across multiple nodes.

You can let the library operate cameras through `Session`, or add synchronization to an existing capture pipeline through `SyncSession`. Your application chooses how to process, display, and record images. The applications in [`examples/`](examples/README.md) show complete integrations, including capture and recording pipelines.

For everyday use, install the package and choose an example script as your application entry point. The native implementation, tests, and build tools in this repository maintain the library; your application imports `bcamsync` and uses the features it needs.

## Start here: one Pi 5, two B0267 boards

The lab starting point is Raspberry Pi 5 with two Arducam B0267 boards and the working vendor camera stack on Raspberry Pi OS Bookworm arm64. [`opencv_sync.py`](examples/opencv_sync.py) is a self-contained application: it owns libcamera capture, attaches synchronization, and optionally displays or records images with OpenCV. This path has been exercised on the devices, including synchronization stop/restart while capture continues.

Follow [installation](docs/installation.md#python) once, including the OpenCV dependencies. From the repository root, copy the configuration and enumerate the board IDs:

```sh
cp examples/configs/one_node_two_boards_pi5.json group.json
.venv/bin/python -c 'from bcamsync import enumerate_arducam_cameras; print(enumerate_arducam_cameras("arducam_b0267_pi5"))'
```

Replace the two `camera_id` placeholders in `group.json` with those IDs. Run a short synchronized recording:

```sh
mkdir -p results
.venv/bin/python examples/opencv_sync.py group.json \
  --seconds 5 --repeat 1 --video-dir results/mjpg --output results/sync.json
```

After synchronization is ready, this writes one 1280×200 MJPG AVI per board and a synchronization report. The script contains the application pipeline; the installed package supplies synchronization. To reuse this application elsewhere, copy `opencv_sync.py` and your completed `group.json`, then run the script with the environment containing `bcamsync`.

The [first-run guide](docs/getting-started.md) explains installation, a longer synchronization check, output files, readiness, and the managed capture alternative. The example currently requires exactly one node and two boards. Other topologies have configuration templates and separate device validation requirements.

## Choose an integration

| Your application | Library entry point | Start with |
| --- | --- | --- |
| Let the library configure cameras and deliver frames | `Config` + `Session` | [`capture.py`](examples/capture.py) |
| Keep your own acquisition, buffers, and recording pipeline | `SyncConfig` + `SyncSession` + `SyncUnit` adapters | [`opencv_sync.py`](examples/opencv_sync.py) for real Pi 5 capture; [`external_capture.py`](examples/external_capture.py) for a synthetic adapter |
| Run browser preview and Pi-local recording | `Session` composed with example application code | [`preview_record.py`](examples/preview_record.py) and [`preview.py`](examples/preview.py) |

Both library entry points share the synchronization runtime. With `Session`, exiting closes acquisition. With `SyncSession`, stopping restores frame-period controls and leaves application-owned capture running. See the [integration guide](docs/integration.md) for lifecycle, timing, frame access, and adapter requirements.

## User guides and examples

- [First run on Pi 5](docs/getting-started.md): configure boards, run synchronization, and save a sample recording.
- [Installation](docs/installation.md): Python, OpenCV, C++, and builds for an external camera SDK.
- [Integration guide](docs/integration.md): choose ownership and connect synchronization to your application.
- [Example catalog](examples/README.md): select an application and identify the files to copy.
- [Optional deployment applications](docs/deployment.md): remote preview/recording, multi-node operation, and independently running networking.

The managed camera backend targets B0267 four-camera boards on Raspberry Pi 4 and Pi 5. Images are consumed locally on the node hosting each board. A board is one synchronization unit; managed frame packets expose its four camera views. Single-node sessions use the local clock and require no network setup.

## Development and validation

This repository includes library source, bindings, tests, application examples, and build tools. [Design and implementation details](docs/design.md) explain the internal boundaries. The [device validation tool](tools/validate_device.py) exercises real-device workflows; software checks and SDK builds are described in [tools/build.sh](tools/build.sh) and [tools/container.sh](tools/container.sh).

Synchronization status is based on sensor timestamps. The default readiness targets, p95 ≤ 0.5 ms and max ≤ 1 ms over two settled windows, are acceptance thresholds. Device timing measurements, optical alignment, and sustained recording throughput are separate checks. See the [first-run guide](docs/getting-started.md#understand-the-result) before using a new setup for experiments.

## Contributors

Tangtangfang Fang (tangtangfang_fang@brown.edu)
