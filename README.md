# B-CamSync

B-CamSync is a camera synchronization library with C++ and Python interfaces. This library measures frame-phase differences, applies bounded frame-period corrections through camera adapters, and reports synchronization quality within one node or across multiple nodes.

**Start with `SyncSession` to add synchronization to your existing capture pipeline.** Your application continues to configure cameras, acquire frames, own image buffers, and record video. This library consumes sensor timing and requests timing corrections through your `SyncUnit` adapters; it does not take over acquisition in this integration.

[`examples/opencv_sync.py`](examples/opencv_sync.py) is a complete application showing this integration on one Pi 5 with two Arducam B0267 boards. The script operates libcamera, provides the adapters, and implements optional OpenCV display and MJPG recording. Installing this library and copying that script plus a configuration gives you a runnable application. Source, bindings, tests, and build tools in this repository are used to develop this library.

## First use: Pi 5, two boards, `SyncSession`

Use Raspberry Pi OS Bookworm arm64 with a working B0267 camera stack and compatible Python libcamera bindings. The lab has exercised this example on this topology, including synchronization restart while capture continues. The [first-run guide](docs/getting-started.md) walks through each step and explains the outputs.

1. Open a terminal in this repository's root and complete the [SyncSession installation](docs/installation.md#python). It builds the synchronization interfaces with managed capture disabled; libcamera development headers, NetworkManager, and chrony are unnecessary for this single-node workflow.
2. Enumerate cameras through your application's Python libcamera stack and copy the external-capture configuration:

```sh
.venv/bin/python -c 'import libcamera; manager = libcamera.CameraManager.singleton(); print([camera.id for camera in manager.cameras])'
cp examples/configs/external_one_node_pi5.json group.json
```

3. Replace the two `camera_id` placeholders in `group.json` with the distinct board IDs. Run synchronization without recording first:

```sh
mkdir -p results
.venv/bin/python examples/opencv_sync.py group.json \
  --seconds 60 --repeat 2 --output results/sync.json
```

4. Save a short recording into a new directory:

```sh
.venv/bin/python examples/opencv_sync.py group.json \
  --seconds 5 --repeat 1 --video-dir results/mjpg-01 --output results/recording-sync.json
```

The script waits for readiness before recording. It writes `board-a1.avi` and `board-a2.avi`, each a 1280×200 MJPG strip containing the board's four view positions, plus a JSON synchronization report. Check `passed: true` in the report and that both videos contain frames. The script implements recording; this library supplies the synchronization attached to its running capture.

The example requires exactly one node and two boards. It uses their shared local clock and runs as one process. To reuse it in another project, copy `opencv_sync.py` and your completed `group.json`, and run it with the environment containing `bcamsync`, Python libcamera, and OpenCV.

For live browser preview without XQuartz, run the same script on the Pi with `--http-preview --continuous --repeat 1`, then forward its loopback HTTP port with SSH. The page shows both boards and synchronization status; JPEG encoding runs in a separate thread at 10 fps by default. See [browser preview over SSH](docs/getting-started.md#browser-preview-over-ssh) for commands.

## Integrate into your project

| Your application | API | Guide/example |
| --- | --- | --- |
| Add synchronization to your existing acquisition and recording pipeline; recommended starting point | `SyncConfig` + `SyncSession` + `SyncUnit` adapters | [Integration guide](docs/integration.md), [`opencv_sync.py`](examples/opencv_sync.py) |
| Learn the adapter with synthetic acquisition | `SyncSession` | [`external_capture.py`](examples/external_capture.py) |
| Have this library configure cameras and deliver local frames; optional managed integration | `Config` + `Session` | [Managed capture](docs/integration.md#optional-managed-capture-session), [`capture.py`](examples/capture.py) |

`SyncSession.stop()` restores baseline frame-period controls while your application keeps capture running. The optional `Session` entry point also owns camera lifecycle and frame delivery; it requires a build with the managed Arducam backend enabled. Both entry points use the same synchronization runtime. See [the example catalog](examples/README.md) for complete applications and the files needed to copy each one.

## Guides

- [First run on Pi 5](docs/getting-started.md): board IDs, synchronization, recording, and output checks.
- [Installation](docs/installation.md): the recommended SyncSession build, optional managed capture, and C++ linking.
- [Integration guide](docs/integration.md): adapter methods, timing observations, control restoration, and lifecycle.
- [Optional deployment applications](docs/deployment.md): browser preview/recording, multi-node operation, and persistent networking.

Synchronization status is based on sensor timestamps. Default readiness targets are p95 ≤ 0.5 ms and max ≤ 1 ms over two settled windows. These are acceptance thresholds; optical alignment and sustained recording throughput require their own measurements. [Design notes](docs/design.md), [device validation](tools/validate_device.py), and [build tools](tools/build.sh) cover development and validation.

## Contributors

Tangtangfang Fang (tangtangfang_fang@brown.edu)
