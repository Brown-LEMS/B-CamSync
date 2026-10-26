# Pi 5 Python examples

These two standalone files use application-owned libcamera capture and local OpenCV windows. Each B0267 board supplies four views in one grayscale strip. Both examples use a 5120×800, 8-bit sensor mode, a 1280×200 preview, 40 fps, 10 ms exposure, and analogue gain 1.0. Change those constants in the script when adapting the application.

| Hardware | File | Synchronization |
| --- | --- | --- |
| One Pi 5, one B0267 board | [`single_board.py`](single_board.py) | None: one board is already one synchronization unit; this script does not import this library |
| One Pi 5, two B0267 boards | [`opencv_sync.py`](opencv_sync.py) | `SyncSession` with one reusable `CameraTiming` adapter per board |

Complete [installation](../docs/installation.md#python) with the compatible vendor camera stack. Run these applications in a graphical session on the Pi; OpenCV needs a working display. Enumerate camera IDs:

```sh
.venv/bin/python -c 'import libcamera; manager = libcamera.CameraManager.singleton(); print([camera.id for camera in manager.cameras])'
```

Pass one board ID for the single-board preview:

```sh
.venv/bin/python examples/single_board.py 'FIRST_CAMERA_ID'
```

Pass two distinct board IDs for synchronized preview:

```sh
.venv/bin/python examples/opencv_sync.py 'FIRST_CAMERA_ID' 'SECOND_CAMERA_ID'
```

Replace the quoted placeholders with the IDs printed by libcamera. No JSON configuration or additional example files are needed. Press q or Esc in an OpenCV window, or Ctrl+C in the terminal, to exit.

The dual-board example constructs its single-node configuration in `sync_config()` and uses the first board as the phase reference. The application submits requests and reports sensor metadata through `CameraTiming`; this library chooses frame-period corrections automatically. The capture thread continues during synchronization shutdown so the adapter can confirm baseline restoration before cameras close.

The terminal reports synchronization state, readiness, phase, p95, and maximum error once per second. Preview is available during startup and continues if synchronization stops or faults; a visible image alone does not establish synchronization. Readiness uses sensor timestamps and does not replace optical validation. See [the first-run guide](../docs/getting-started.md) for interpretation.

Recording, browser preview, SSH preview, and managed capture applications live in [`tools/`](../tools/README.md). Configuration templates for those tools and multi-node deployments live in [`docs/configs/`](../docs/configs). Synthetic acquisition fixtures and CMake installation checks live in `tests/`.
