# Example applications

[User guide](../README.md) · [Pi 5 first run](../docs/getting-started.md) · [Integration guide](../docs/integration.md)

Start with `opencv_capture.py` to attach this library's `SyncSession` interface to application-owned capture. The script provides a complete libcamera acquisition and OpenCV recording pipeline and imports the reusable `CameraTiming` class from `bcamsync.adapters.libcamera`. You can copy that one script and its configuration, or import the same adapter and connect it to request submission/completion in your existing pipeline. `synthetic_capture.py` demonstrates implementing a new adapter through `bcamsync.adapters`. The other examples offer separate applications and optional managed capture.

## Choose an application

| Application | What it demonstrates | Files to copy alongside your configuration |
| --- | --- | --- |
| [`opencv_capture.py`](../tools/opencv_capture.py) | Real Pi 5 single-node, two-board libcamera capture with `SyncSession`; optional OpenCV windows and short MJPG AVI recording | `opencv_capture.py` |
| [`synthetic_capture.py`](../tests/synthetic_capture.py) | Synthetic acquisition with a complete `SyncUnit` adapter and application-owned lifecycle | `synthetic_capture.py`; configuration is constructed inside the script |
| [`capture.py`](../tools/managed/capture.py) | Optional managed `Session` acquisition and local four-view frame reads; no recording; requires the managed backend | `capture.py` |
| [`preview_record.py`](../tools/managed/preview_record.py) | Managed capture, browser controls/preview, and Pi-local full-resolution MJPG + JSONL recording | `preview_record.py` and `preview.py` in the same directory |
| [`preview.py`](../tools/managed/preview.py) | Managed capture with browser preview and remote start/stop | `preview.py` |
| [`manage_group.py`](../tools/manage_group.py) | Workstation control of node applications | `manage_group.py` and the separately installed `bcamsync_host` package |
| [`capture.cpp`](../tools/managed/capture.cpp), [`cmake_consumer/`](../tests/installed_managed) | C++ managed capture and linking against an installed library | Adapt these sources and their CMake build |

All node applications use the installed `bcamsync` core package; the workstation client uses `bcamsync_host`. `opencv_capture.py` additionally requires Python libcamera, OpenCV, and NumPy. `capture.py`, the node service, and managed preview/recording applications require the separately installed `bcamsync-managed` add-on; their existing explicit managed imports from `bcamsync` remain compatible. Managed preview applications also require Pillow. See [installation](../docs/installation.md) for the separate installation commands.

The recommended Pi 5 application is self-contained in one script. For example, after copying it and your completed configuration into your own project and activating the appropriate environment:

```sh
mkdir -p results
python opencv_capture.py group.json --seconds 5 --repeat 1 \
  --video-dir results/mjpg-01 --output results/sync.json
```

The [first-run guide](../docs/getting-started.md) explains the exact hardware/configuration and outputs. For remote preview or recording, follow the [deployment guide](../docs/deployment.md). Choose new recording directories for each OpenCV run to preserve existing AVI files.

## Configuration templates

Copy a template from [`configs/`](../docs/configs) and replace camera IDs with those enumerated through your Python libcamera stack. Node and unit names are logical identifiers; each B0267 board is one unit with four view positions. The recommended external-capture template leaves SDK settings with the script. Managed Pi 5 capture uses a separate template with `arducam_b0267_pi5` profiles.

| Template | Intended use |
| --- | --- |
| [`external_one_node_pi5.json`](../docs/configs/external_one_node_pi5.json) | Recommended Pi 5 dual-board `SyncSession` application; camera IDs are consumed by the script |
| [`one_node_two_boards_pi5.json`](../docs/configs/one_node_two_boards_pi5.json) | Optional managed Pi 5 dual-board capture with explicit backend profiles |
| [`one_node_two_boards.json`](../docs/configs/one_node_two_boards.json) | Single-node, dual-board managed capture with the default Pi 4 profile |
| [`two_nodes_one_board.json`](../docs/configs/two_nodes_one_board.json), [`two_nodes_one_board_pi5.json`](../docs/configs/two_nodes_one_board_pi5.json) | Two nodes, one board each, with the matching managed profile |
| [`two_nodes_two_boards.json`](../docs/configs/two_nodes_two_boards.json) | Two nodes, two boards each |
| [`external_two_nodes.json`](../docs/configs/external_two_nodes.json) | Synchronization topology for application-owned capture adapters |
| [`fake_one_node.json`](../tests/fixtures/configs/fake_one_node.json), [`fake_two_nodes.json`](../tests/fixtures/configs/fake_two_nodes.json) | Software acquisition checks with explicit fake backends |
| [`host_targets.json`](../docs/configs/host_targets.json) | Optional SSH targets for workstation management |

Templates describe supported configuration shapes; a template is not a device measurement. The OpenCV script enforces one node and two units. Extending that application to more units requires changing its application constraints and validating the actual setup.
