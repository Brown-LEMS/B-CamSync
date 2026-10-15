# First run: one Pi 5, two B0267 boards

[User guide](../README.md) · [Installation](installation.md) · [Integration guide](integration.md)

This guide uses the real-device example [`opencv_sync.py`](../examples/opencv_sync.py). One Python file owns camera capture, synchronization attachment, optional local display, and MJPG recording. Once B-CamSync is installed, you can copy this script and a completed configuration into your project. The lab has exercised this path on a Pi 5 with two B0267 boards, including stopping and restarting synchronization while acquisition continues.

## 1. Install once

Use Raspberry Pi OS Bookworm arm64 with a working vendor camera stack and matching libcamera development files. Complete the [Python installation](installation.md#python), including its OpenCV subsection. Run the following commands from the repository root with that environment's `.venv/bin/python`.

## 2. Identify the boards

```sh
cp examples/configs/one_node_two_boards_pi5.json group.json
.venv/bin/python -c 'from bcamsync import enumerate_arducam_cameras; print(enumerate_arducam_cameras("arducam_b0267_pi5"))'
```

Replace `REPLACE_WITH_FIRST_CAMERA_ID` and `REPLACE_WITH_SECOND_CAMERA_ID` in `group.json` with the two returned camera IDs. Keep `node-a` as the local node, `board-a1` and `board-a2` as the unit IDs, and `board-a1` as the reference. These are logical names; `camera_id` binds a unit to the physical device. The reference is explicitly configured. There is no node or reference election.

One B0267 board is one synchronization unit with four camera view positions. Two boards therefore expose eight view positions. This example requires exactly one node and two boards; it uses their shared local clock without network setup, a workstation service, or a separate node process.

The template selects 40 fps and the Pi 5 profile. In this application, `Capture` configures libcamera directly: a 5120×800 sensor mode, a 1280×200 YUV420 output strip, fixed 10000 µs exposure, and 1× gain. The template's managed acquisition settings do not configure this external capture path; edit `Capture` to change its image format, exposure, or gain. This example does not run the managed backend's four-view gain calibration.

## 3. Check synchronization and restart

```sh
mkdir -p results
.venv/bin/python examples/opencv_sync.py group.json \
  --seconds 60 --repeat 2 --output results/sync.json
```

Each run waits for readiness and prints synchronization state, phase, p95, max, sensor-based frame rate, and correction counts. The duration starts after readiness. At the end of each run, the script stops synchronization and checks that the camera sequences continue advancing. The second run starts synchronization again on the same running cameras. The JSON report stores the configuration, readiness policy, samples, and run results.

Add `--display` when local OpenCV windows are available. Display and recording are off by default.

## 4. Save a short recording

Choose a new output directory for each recording; the example names its files by unit ID.

```sh
.venv/bin/python examples/opencv_sync.py group.json \
  --seconds 5 --repeat 1 --video-dir results/mjpg --output results/recording-sync.json
```

Recording starts after readiness. The output directory contains `board-a1.avi` and `board-a2.avi`: one 1280×200 MJPG strip per board, preserving all four view positions horizontally. The JSON file is the run's synchronization report. Video output requires `--repeat 1`; the example closes the writers when capture ends. Its check that capture continues after synchronization stops also leaves the writers active briefly during shutdown, so the video length can slightly exceed `--seconds`.

The AVI files use the configured nominal frame rate and do not contain per-frame sensor timestamp metadata. Keep the JSON report for synchronization status. If your experiment needs full-resolution streams with per-frame metadata, use the separate [preview and recording application](deployment.md#one-pi-5-two-boards-remote-preview-and-mjpg-recording), which uses managed capture and writes MJPG plus JSONL.

## Understand the result

A successful run reports `passed: true`, reaches readiness for each requested run, and records `capture_continued_after_sync_stop: true`. A timeout, capture error, synchronization fault, or sampled loss of readiness causes this example to raise an error. When `--output` is supplied, runs that reach the capture loop also write the report during cleanup, including failed runs.

The default readiness policy requires p95 ≤ 0.5 ms and max ≤ 1 ms over two settled windows. These are configured acceptance thresholds. The reported phase metrics compare sensor timestamps; reported fps is estimated from sensor timestamp history. Neither measures optical exposure alignment, video encoding throughput, or storage throughput. Repeat the check on the actual boards and configuration used by your project, then measure recording performance and optical alignment as your experiment requires.

## Use managed capture instead

Choose `Session` when you want B-CamSync to configure the cameras and deliver their frames. With the same completed Pi 5 configuration and the managed Arducam backend installed, run:

```sh
.venv/bin/python examples/capture.py group.json node-a --seconds 30
```

This application prints local frame sequences, four-view shapes, drops, clock validity, and synchronization status. It starts managed acquisition and reports status while frames arrive; it does not wait for readiness or record video. Managed capture includes startup gain calibration across the four views. Select this path when that ownership and calibration fit your application. See the [integration guide](integration.md) for a small frame-reading loop and lifecycle details.

## Bring the application into your project

For the OpenCV workflow, copy `opencv_sync.py` and your completed `group.json`, activate the environment containing B-CamSync and the OS camera modules, and run `python opencv_sync.py group.json` with your desired options. Change the script's image processing and recording code to fit your experiment; synchronization continues through its `CameraTiming` adapters and `SyncSession`.

For a library-owned camera pipeline, copy `capture.py` and the configuration, then add processing after `Session.read()`. For an existing acquisition system, follow [the adapter guide](integration.md#attach-to-an-existing-pipeline-syncsession). The [example catalog](../examples/README.md) lists each application's dependencies, including the two-file browser recording application.
