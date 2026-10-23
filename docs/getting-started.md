# First run on Pi 5

[User guide](../README.md) · [Installation](installation.md) · [Integration](integration.md) · [Examples](../examples/README.md)

Start with one board to check capture, then add a second board and synchronization. The two examples are independent Python files. Both own their cameras and buffers and only display OpenCV windows. The dual-board example attaches this library's `SyncSession` using reusable `CameraTiming` adapters; this library decides the corrections.

## 1. Prepare the Pi

Use Raspberry Pi OS Bookworm arm64, the working Arducam B0267 vendor camera stack, and its matching Python libcamera bindings. Complete the [Python installation](installation.md#python). The examples require `SensorConfiguration`, `ExposureTimeMode`, and `AnalogueGainMode` in those bindings. Use the project venv with system site packages so libcamera and the OS OpenCV installation are visible.

Run these examples from a terminal in the Pi's graphical desktop with a working OpenCV display. A plain SSH session does not provide that display. For windows on a development workstation, use the separate [SSH preview tool](../tools/README.md#remote-desktop-preview).

Commands below run from the repository root. Each B0267 board is one synchronization unit containing four camera views. The scripts select a 5120×800, 8-bit sensor mode and display its four-view strip at 1280×200, with 40 fps, 10 ms exposure, and gain 1.0. Keep those settings identical on both boards.

## 2. Identify the boards

Enumerate IDs using the same Python camera stack as the application:

```sh
.venv/bin/python -c 'import libcamera; manager = libcamera.CameraManager.singleton(); print([camera.id for camera in manager.cameras])'
```

Copy each complete board ID, including any leading slash, into the quoted arguments below. Two boards must have distinct IDs. Identify their physical positions before choosing which should be the reference.

## 3. Preview one board

```sh
.venv/bin/python examples/single_board.py 'FIRST_CAMERA_ID'
```

One window shows all four views as a grayscale strip. This script does not import this library: there is no second synchronization unit to align. It checks the application's SDK capture and pixel path. Press q or Esc in the window, or Ctrl+C in the terminal, to close the camera.

## 4. Preview and synchronize two boards

```sh
.venv/bin/python examples/opencv_sync.py 'FIRST_CAMERA_ID' 'SECOND_CAMERA_ID'
```

The first argument becomes `board-1`, the reference; the second becomes `board-2`. The script constructs one node named `pi5` and its two units directly in `sync_config()`. No configuration file, network setup, managed capture add-on, or other example file is required.

Two OpenCV windows display live acquisition throughout startup. The terminal prints the synchronization state and `ready` flag once per second, followed by each unit's phase, p95, and maximum error in milliseconds. Wait for `ready=True`; early zero metrics are not a successful measurement. This library continues tracking and correcting phase while the preview runs.

Default readiness thresholds are p95 ≤ 0.5 ms and max ≤ 1 ms over two settled windows. They are configured acceptance targets based on sensor timestamps. These simplified examples have software checks but still require a fresh run on the physical Pi 5 and B0267 boards; they do not establish an optical result or recording throughput.

If synchronization faults or stops, the library warns, readiness becomes false, and preview continues. Inspect the printed `fault` and state; treat those frames as unsynchronized. The example does not automatically restart synchronization. A capture error ends the application and is reported as an exception.

Press q or Esc in either window, or Ctrl+C in the terminal, to exit. The application first stops synchronization while capture callbacks continue, allowing baseline frame-period restoration to be observed. It then joins the capture thread and closes both cameras.

## Reuse and recording

Copy either script into your project and use the environment containing its camera and image dependencies. The dual-board script additionally needs the installed `bcamsync` core. Its three integration points are `CameraTiming.next_submission()` before queueing each request, `CameraTiming.observe()` on successful completion, and passing both adapters to `SyncSession`.

The previously tested recording and browser-preview application remains available as [`tools/opencv_capture.py`](../tools/opencv_capture.py), with its JSON template in [`configs/external_one_node_pi5.json`](configs/external_one_node_pi5.json). See [developer tools](../tools/README.md) for recording commands and workstation preview over SSH. Those application features are separate from the minimal synchronization example.
