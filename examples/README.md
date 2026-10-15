# Example applications

[User guide](../README.md) · [Pi 5 first run](../docs/getting-started.md) · [Integration guide](../docs/integration.md)

These are application entry points built on the installed B-CamSync library. Choose one that matches your workflow, then copy or adapt it. Capture loops, displays, encoders, file formats, and remote controls in these scripts belong to their example applications. You do not need to assemble all examples into one program.

## Choose an application

| Application | What it demonstrates | Files to copy alongside your configuration |
| --- | --- | --- |
| [`opencv_sync.py`](opencv_sync.py) | Real Pi 5 single-node, two-board libcamera capture with `SyncSession`; optional OpenCV windows and short MJPG AVI recording | `opencv_sync.py` |
| [`capture.py`](capture.py) | Managed `Session` acquisition and local four-view frame reads; no recording | `capture.py` |
| [`external_capture.py`](external_capture.py) | Synthetic acquisition with a complete `SyncUnit` adapter and application-owned lifecycle | `external_capture.py`; configuration is constructed inside the script |
| [`preview_record.py`](preview_record.py) | Managed capture, browser controls/preview, and Pi-local full-resolution MJPG + JSONL recording | `preview_record.py` and `preview.py` in the same directory |
| [`preview.py`](preview.py) | Managed capture with browser preview and remote start/stop | `preview.py` |
| [`serve_node.py`](serve_node.py) | HTTP node controls with automatic network startup and no preview | `serve_node.py`, or use the installed `bcamsync-node` command with `--network-startup automatic` |
| [`manage_group.py`](manage_group.py) | Workstation control of node applications | `manage_group.py` and the separately installed `bcamsync_host` package |
| [`capture.cpp`](capture.cpp), [`cmake_consumer/`](cmake_consumer) | C++ managed capture and linking against an installed library | Adapt these sources and their CMake build |

All Python applications import the installed `bcamsync` package, except the workstation client, which imports `bcamsync_host`. `opencv_sync.py` additionally requires Python libcamera, OpenCV, and NumPy. Preview applications require the optional Pillow dependency. See [installation](../docs/installation.md) for environments and dependencies.

The recommended Pi 5 application is self-contained in one script. For example, after copying it and your completed configuration into your own project and activating the appropriate environment:

```sh
mkdir -p results
python opencv_sync.py group.json --seconds 5 --repeat 1 \
  --video-dir results/mjpg --output results/sync.json
```

The [first-run guide](../docs/getting-started.md) explains the exact hardware/configuration and outputs. For remote preview or recording, follow the [deployment guide](../docs/deployment.md). Choose new recording directories for each OpenCV run to preserve existing AVI files.

## Configuration templates

Copy a template from [`configs/`](configs) and replace camera IDs with those enumerated on your devices. Node and unit names are logical identifiers; each B0267 board is one unit with four view positions. For managed Pi 5 capture, set `profile` to `arducam_b0267_pi5` for every board.

| Template | Intended use |
| --- | --- |
| [`one_node_two_boards_pi5.json`](configs/one_node_two_boards_pi5.json) | Recommended Pi 5 dual-board first run; managed or OpenCV external capture |
| [`one_node_two_boards.json`](configs/one_node_two_boards.json) | Single-node, dual-board managed capture with the default Pi 4 profile |
| [`two_nodes_one_board.json`](configs/two_nodes_one_board.json), [`two_nodes_one_board_pi5.json`](configs/two_nodes_one_board_pi5.json) | Two nodes, one board each, with the matching managed profile |
| [`two_nodes_two_boards.json`](configs/two_nodes_two_boards.json) | Two nodes, two boards each |
| [`external_two_nodes.json`](configs/external_two_nodes.json) | Synchronization topology for application-owned capture adapters |
| [`fake_one_node.json`](configs/fake_one_node.json), [`fake_two_nodes.json`](configs/fake_two_nodes.json) | Software acquisition checks with explicit fake backends |
| [`host_targets.json`](configs/host_targets.json) | Optional SSH targets for workstation management |

Templates describe supported configuration shapes; a template is not a device measurement. The OpenCV script enforces one node and two units. Extending that application to more units requires changing its application constraints and validating the actual setup.
