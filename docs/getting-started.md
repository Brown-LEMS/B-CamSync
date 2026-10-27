# First run on Pi 5

[User guide](../README.md) · [Installation](installation.md) · [Integration](integration.md) · [Examples](../examples/README.md)

For a new Pi or your first SSH connection, start with [From a blank Raspberry Pi to a B0267 demo](pi-from-scratch.md). It covers Imager, SSH credentials, the camera stack, package installation, and browser preview step by step.

Start with one board to check capture, then add a second board and synchronization. Four independent Python examples offer either OpenCV windows or HTTP web preview for each topology. All own their cameras and buffers. The dual-board examples attach this library's `SyncSession` using reusable `CameraTiming` adapters; this library decides the corrections.

## 1. Prepare the Pi

Use Raspberry Pi OS Bookworm arm64, the working Arducam B0267 vendor camera stack, and its matching Python libcamera bindings. Complete the [Python installation](installation.md#python). The examples require `SensorConfiguration`, `ExposureTimeMode`, and `AnalogueGainMode` in those bindings. Use the project venv with system site packages so libcamera and the OS OpenCV installation are visible.

The OpenCV-window examples require a graphical desktop on the Pi. The HTTP examples can run in a plain SSH session without a display: open `http://PI_IP:8000/` in a browser that can reach the Pi. They listen on `0.0.0.0:8000` by default; `--bind` and `--port` change this. For native player windows on a development workstation, the separate [SSH preview tool](../tools/README.md#remote-desktop-preview) remains available.

Commands below run from the repository root. Each B0267 board is one synchronization unit containing four camera views. The scripts select a 5120×800, 8-bit sensor mode and display its four-view strip at 1280×200, with 40 fps, 10 ms exposure, and gain 1.0. Keep those settings identical on both boards.

## 2. Identify the boards

The scripts enumerate cameras automatically, sort IDs, and select the first one or two devices. To inspect the available IDs before capture, run:

```sh
.venv/bin/python examples/single_board.py --list
```

All four scripts support `--list`; it opens no cameras and needs no graphical display or OpenCV/NumPy import. Enumeration lists all libcamera devices, so verify that the selected IDs belong to the B0267 boards when other cameras are attached. To select specific devices, pass one ID to the single-board script or two distinct IDs to the dual-board script; the first explicit ID is the reference.

## 3. Preview one board

```sh
.venv/bin/python examples/single_board.py
```

One window shows all four views as a grayscale strip. To view it in a browser instead, run `.venv/bin/python examples/single_board_http.py`. Neither single-board script imports this library: there is no second synchronization unit to align. They check the application's SDK capture and pixel path. Press q or Esc in the OpenCV window, or Ctrl+C in the Pi terminal, to close the camera.

## 4. Preview and synchronize two boards

```sh
.venv/bin/python examples/opencv_sync.py
```

For HTTP preview, run `.venv/bin/python examples/opencv_sync_http.py` instead. The first selected device becomes `board-1`, the reference; the second becomes `board-2`. Both scripts print their selection and roles at startup. To fix the reference explicitly, pass `'REFERENCE_CAMERA_ID' 'SECOND_CAMERA_ID'` to the chosen script. Each script constructs one node named `pi5` and its two units directly in `sync_config()`. No configuration file, library-managed network setup, managed capture add-on, or other example file is required.

Two OpenCV windows or the HTTP page display live acquisition throughout startup. The terminal prints the synchronization state and `ready` flag once per second, followed by each unit's phase, p95, and maximum error in milliseconds; the HTTP page also shows these fields as JSON. Wait for `ready=True`; early zero metrics are not a successful measurement. This library continues tracking and correcting phase while the preview runs.

Default readiness thresholds are p95 ≤ 0.5 ms and max ≤ 1 ms over two settled windows. They are configured acceptance targets based on sensor timestamps. These simplified examples have software checks but still require a fresh run on the physical Pi 5 and B0267 boards; they do not establish an optical result or recording throughput.

If synchronization faults or stops, the library warns, readiness becomes false, and preview continues. Inspect the printed `fault` and state; treat those frames as unsynchronized. The example does not automatically restart synchronization. A capture error ends the application and is reported as an exception.

Press q or Esc in an OpenCV window, or Ctrl+C in the Pi terminal, to exit. Closing an HTTP browser tab leaves capture and synchronization running; use Ctrl+C on the Pi to stop it. The application first stops synchronization while capture callbacks continue, allowing baseline frame-period restoration to be observed. It then joins the capture thread, closes both cameras, and releases any HTTP server.

## Reuse and recording

Copy either script into your project and use the environment containing its camera and image dependencies. The dual-board script additionally needs the installed `bcamsync` core. Its three integration points are `CameraTiming.next_submission()` before queueing each request, `CameraTiming.observe()` on successful completion, and passing both adapters to `SyncSession`.

The previously tested recording and browser-preview application remains available as [`tools/opencv_capture.py`](../tools/opencv_capture.py), with its JSON template in [`configs/external_one_node_pi5.json`](configs/external_one_node_pi5.json). See [developer tools](../tools/README.md) for recording commands and workstation preview over SSH. Those application features are separate from the minimal synchronization example.
