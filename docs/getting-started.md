# First run: Pi 5 dual-board capture with `SyncSession`

[User guide](../README.md) · [Installation](installation.md) · [Integration guide](integration.md)

This walkthrough installs this library, attaches `SyncSession` to the capture pipeline in [`opencv_sync.py`](../examples/opencv_sync.py), checks synchronization, and records two short videos. The application is one Python file. It owns the cameras and image buffers, passes timing observations to this library through `CameraTiming` adapters, and implements display and recording. This library decides frame-period corrections and reports synchronization status through `SyncSession`.

The lab has exercised this application on one Raspberry Pi 5 with two Arducam B0267 boards, including stopping and restarting synchronization while capture continues. One board is one synchronization unit with four camera view positions; two boards expose eight view positions. This script requires exactly one node and two boards.

## 1. Install once

Open a terminal in the shared repository checkout. Complete [the recommended Python installation](installation.md#python) using Raspberry Pi OS Bookworm arm64, the working B0267 vendor camera stack, and compatible Python libcamera bindings. That installation builds only the synchronization core, which is sufficient for `SyncSession` and this application. The managed capture add-on is unnecessary. It uses a venv with system site packages and `numpy<2` for OpenCV.

Keep this terminal in the repository root for the following commands. Each command explicitly uses `.venv/bin/python`; the module import check in the installation guide must pass first. The camera drivers and libcamera stack must already support your boards; installing this library does not install that device software.

## 2. Identify the boards

Close other applications using the cameras. Enumerate camera IDs using the same Python libcamera module that the example will use:

```sh
.venv/bin/python -c 'import libcamera; manager = libcamera.CameraManager.singleton(); print([camera.id for camera in manager.cameras])'
cp examples/configs/external_one_node_pi5.json group.json
```

This uses the Python `CameraManager.cameras` interface, also used by the [upstream Python capture application](https://github.com/raspberrypi/libcamera/blob/main/src/py/cam/cam.py). Expect two distinct board IDs. Replace `REPLACE_WITH_FIRST_CAMERA_ID` and `REPLACE_WITH_SECOND_CAMERA_ID` in `group.json` with those exact IDs, preserving JSON quotes and commas. Each board needs its own ID.

Keep `node-a` as the local node, `board-a1` and `board-a2` as the unit IDs, and `board-a1` as the reference. These names are logical identifiers. `SyncConfig` reads membership, reference, and frame rate; the script's `Capture` class reads `camera_id` to open each device. The reference is explicitly configured. The group uses one local clock and requires no network setup or separate node service.

The configuration selects 40 fps. The script configures a 5120×800 sensor mode, a 1280×200 YUV420 output strip, fixed 10000 µs exposure, and 1× gain. To change image format, exposure, or gain, edit the script's `Capture` class. This external integration leaves those settings with the application. This library does not run managed-camera gain calibration on this path.

## 3. Check synchronization and restart

```sh
mkdir -p results
.venv/bin/python examples/opencv_sync.py group.json \
  --seconds 60 --repeat 2 --output results/sync.json
```

The script starts acquisition, constructs one timing adapter per board, and attaches `SyncSession`. Each run waits for readiness before starting the requested 60-second interval. It prints state, readiness, phase, p95, max, sensor-based frame rate, and correction counts. At each run's end, it stops synchronization and checks that both camera sequences keep advancing. The second run restarts synchronization on those same running cameras.

Expect two `Run N: sync stopped; external capture continues` messages and normal process exit. Check the JSON report:

```sh
.venv/bin/python - <<'PY'
import json
from pathlib import Path

report = json.loads(Path("results/sync.json").read_text())
assert report["passed"]
assert len(report["runs"]) == 2
assert all(run["capture_continued_after_sync_stop"] for run in report["runs"])
print("Both synchronization runs passed; capture continued after each sync stop")
PY
```

A failed assertion means the expected check did not pass. A readiness timeout, capture error, synchronization fault, or sampled loss of readiness raises an error in the example. When `--output` is supplied, runs that reach its capture loop write a report during cleanup, including failed runs. Read the terminal error and report before recording.

## 4. Record and check both videos

Choose a new video directory for every run; this example's fixed filenames can overwrite an earlier recording. Keep `--repeat 1` when recording:

```sh
.venv/bin/python examples/opencv_sync.py group.json \
  --seconds 5 --repeat 1 --video-dir results/mjpg-01 --output results/recording-sync.json
```

The script opens the writers after readiness and writes `results/mjpg-01/board-a1.avi` and `results/mjpg-01/board-a2.avi`. Each file is a 1280×200 MJPG strip with its board's four view positions arranged horizontally. Check the report and decode the first frame of each video:

```sh
.venv/bin/python - <<'PY'
import json
from pathlib import Path
import cv2

report = json.loads(Path("results/recording-sync.json").read_text())
assert report["passed"] and len(report["runs"]) == 1
assert report["runs"][0]["capture_continued_after_sync_stop"]
for unit_id in ("board-a1", "board-a2"):
    path = Path("results/mjpg-01") / f"{unit_id}.avi"
    video = cv2.VideoCapture(str(path))
    try:
        ok, frame = video.read()
        assert ok and frame.shape[:2] == (200, 1280), path
        print(f"{path}: first frame decoded, shape={frame.shape}")
    finally:
        video.release()
PY
```

This checks that the synchronization run passed and that both files contain readable images. The writers close when the script ends capture. They remain active briefly during its post-stop capture check, so the videos can slightly exceed the requested five seconds. Add `--display` only when local OpenCV windows are available; headless recording uses no display flag.

## Browser preview over SSH

The same external-capture example can serve both boards to your workstation's browser. It uses Python's standard-library HTTP server and OpenCV JPEG encoding, with no local X11 server or additional Python dependency. Run on the Pi:

```sh
.venv/bin/python examples/opencv_sync.py group.json \
  --http-preview --continuous --repeat 1
```

In a workstation terminal, establish the SSH tunnel, replacing the user and address for your Pi:

```sh
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:8765:127.0.0.1:8765 bapd8@192.168.0.106
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765) in the workstation browser. The Pi HTTP service binds only to `127.0.0.1`. The page shows the latest image from each board during calibration and tracking, and updates synchronization status once per second. Frames older than one second are hidden. Closing the browser or tunnel leaves the Pi application running; use Ctrl+C in its terminal to stop synchronization and capture.

Preview defaults to 10 fps independently of the configured 40 fps acquisition. `--preview-fps 5` reduces JPEG refresh frequency, and `--http-port` changes the Pi listening port; update the tunnel's destination port accordingly. Encoding and HTTP writes run on separate threads from acquisition. Continuous mode runs one synchronization session until interrupted and does not accumulate a test-sample history. Omit `--continuous` to retain the timed test runs. Recording remains off unless `--video-dir` is supplied.

## Understand the result

The JSON report stores configuration, readiness policy, run results, and sampled synchronization status. Default readiness requires p95 ≤ 0.5 ms and max ≤ 1 ms over two settled windows. These are acceptance thresholds, separate from measurements. Phase metrics compare sensor timestamps, and reported fps is estimated from sensor timestamp history. Optical alignment and sustained recording/storage throughput require separate measurements on your experiment's setup.

The AVI files use the nominal frame rate and contain no per-frame sensor timestamp metadata. The JSON report contains periodic status samples, rather than a timestamp for every recorded image. If you need per-frame metadata in your existing pipeline, store the source timestamps there; the [integration guide](integration.md#timing-observations) explains source and mapped time. An optional [managed browser recording example](deployment.md#one-pi-5-two-boards-remote-preview-and-mjpg-recording) provides full-resolution MJPG plus JSONL with different camera ownership and dependencies.

## Reuse this integration in your project

Copy `opencv_sync.py` and the completed `group.json` into your project. Activate the environment containing this library, Python libcamera, and OpenCV, then run `python opencv_sync.py group.json` with the same options. Other example files, node services, and a workstation client are unnecessary for this application.

If you already have a capture pipeline, retain it and adapt the `CameraTiming` class to its sensor metadata and per-request frame-period controls. Keep your image processing and recording path. Follow [the SyncSession integration guide](integration.md#attach-to-an-existing-pipeline-syncsession) for the adapter contract and shutdown order. The optional [managed integration](integration.md#optional-managed-capture-session) is available when you want this library to own acquisition instead.
