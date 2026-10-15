# Optional deployment and recording applications

[Back to the user guide](../README.md) · [Single-node first run](getting-started.md) · [Example catalog](../examples/README.md)

These applications compose synchronization with network setup, remote controls, preview, and recording. Choose the workflow your application needs. The single-node OpenCV example runs with an installed package, one script, and one configuration; its [first-run guide](getting-started.md) is the recommended starting point for the Pi 5 lab setup.

All commands below run from the repository root. Example scripts and the separate host package are source files in this repository.

## Two Raspberry Pi 4 nodes

Use one B0267 board with four cameras on each Pi 4. Install B-CamSync on both nodes using the [installation guide](installation.md), then connect their Ethernet ports directly.

On each node, copy the configuration and enumerate its local board ID:

```sh
cp examples/configs/two_nodes_one_board.json group.json
.venv/bin/python -c 'from bcamsync import enumerate_arducam_cameras; print(enumerate_arducam_cameras())'
```

Replace both `camera_id` placeholders with the IDs returned on the respective nodes, and put the completed `group.json` on both. The example names the nodes `node-a` and `node-b`, uses `eth0`, and selects `board-a1` as the reference. Adjust the interface names if needed. Pi 4 is the default profile; automatic networking reuses a usable interface IPv4 address or creates a temporary link-local connection.

One launch sequence is to start the follower first, then the reference in a terminal on the other Pi. Either node may start first; all configured peers must arrive within the startup deadline.

```sh
# On node-b:
.venv/bin/python examples/capture.py group.json node-b --seconds 30
# On node-a:
.venv/bin/python examples/capture.py group.json node-a --seconds 30
```

Each node acquires its local board and prints frame sequences, four-view shapes, and synchronization status. The reference ends the group after 30 seconds. The default settings are 40 fps, 10000 µs exposure, and startup gain calibration. Other layouts are in [examples/configs](../examples/configs).

## Optional network infrastructure

Networking can run independently of cameras. On each node, this installed command starts the local network and continuously announces the node, including while other nodes are offline:

```sh
.venv/bin/bcamsync-network group.json node-a
```

It reads only synchronization configuration and never initializes cameras or starts clock estimation. Run it from your node application or process supervisor when persistent networking is useful. SIGINT/SIGTERM releases only its owned network resources. Later camera sessions using automatic networking reuse the established IP; stopping capture leaves this network process running.

For an HTTP control endpoint with independently owned networking, use:

```sh
.venv/bin/bcamsync-node group.json node-a --network-startup automatic
```

HTTP discovery and configuration/status queries are available before peers or cameras start. An explicit `start` request waits for group discovery and starts capture and synchronization; `stop` keeps the network and HTTP endpoint running. A peer-discovery timeout leaves networking available for a later request. Status includes the network address and any background discovery fault.

The node command defaults to `--network-startup explicit`. In that mode it uses an already available HTTP address, supplied with `--address` or the local node's configured address; networking for synchronization starts only when capture is explicitly started. `network.mode: "auto"` versus `"existing"` continues to select address setup, independently of the command's startup policy. Applications can also use `NetworkSession.start()`, `resolve()`, and `stop()` directly, and pass a started network to `NodeService(..., network=network)`; the application retains ownership. Supplied external `SyncSession` instances use the same controller path.

## Browser preview

After the capture example finishes, connect both nodes and your workstation to the same Ethernet switch with mutually reachable IPv4 addresses. If automatic networking created link-local connections on the Pis, give the workstation an IPv4 link-local address on its Ethernet interface. If the Pis reuse existing addresses, use a workstation address on their reachable network.

Install the preview dependency and start the example on each Pi, using its own node ID:

```sh
.venv/bin/python -m pip install '.[preview]'
.venv/bin/python examples/preview.py group.json node-a
# On the other Pi, use node-b instead.
```

On the workstation, run these commands from a checkout of this repository to install the host client. Copy `group.json` there as well:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install ./host
.venv/bin/python examples/manage_group.py group.json discover
.venv/bin/python examples/manage_group.py group.json start
```

Open the URLs returned by `discover` to see each node's four camera views. Discovery works before cameras start; the `start` command starts the whole group. To select a workstation adapter, add `--interface en7` to the host commands, replacing `en7` with your interface name. To end acquisition, run:

```sh
.venv/bin/python examples/manage_group.py group.json stop
```

Closing the browser leaves acquisition running. Preview is an independent example application that consumes frames; HTTP forwards control requests, while the node application owns capture and networking.

## One Pi 5, two boards: remote preview and MJPG recording

The [preview_record.py](../examples/preview_record.py) application combines browser preview with Pi-local MJPEG recording. It imports [preview.py](../examples/preview.py); keep both files together if you copy this application into another project. Recording is downstream example code; the library provides frame access and synchronization. Each board writes one full-resolution 5120×800 grayscale JPEG stream containing its four views side by side, giving eight camera views across the two boards. Preview is resized and sampled independently; recording processes every packet read by the application.

Install the package with its preview dependency on the Pi using `.venv/bin/python -m pip install '.[preview]'`. Copy the Pi 5 template and enumerate the two board IDs:

```sh
cp examples/configs/one_node_two_boards_pi5.json group.json
.venv/bin/python -c 'from bcamsync import enumerate_arducam_cameras; print(enumerate_arducam_cameras("arducam_b0267_pi5"))'
```

Replace both `camera_id` placeholders in `group.json`; both units explicitly select `arducam_b0267_pi5`. Start the application with the Pi's existing IPv4 address and a writable recording directory on the Pi, replacing the example address and path:

```sh
.venv/bin/python examples/preview_record.py group.json node-a \
  --address 192.168.1.50 --output-dir /mnt/ssd/bcamsync
```

Open `http://192.168.1.50:58083` on your workstation. Click **Start node** for both boards' preview, then **Start recording**. **Stop recording** closes the files while preview continues; **Stop node** stops capture and closes any recording. Closing the browser leaves capture and recording running. The following commands provide the same control from the workstation without installing the host package:

```sh
curl --fail-with-body -X POST http://192.168.1.50:58083/start
curl --fail-with-body -X POST http://192.168.1.50:58083/record/start
curl --fail-with-body http://192.168.1.50:58083/status
# After the desired recording interval:
curl --fail-with-body -X POST http://192.168.1.50:58083/record/stop
curl --fail-with-body -X POST http://192.168.1.50:58083/stop
```

Each recording creates a new UUID directory containing `manifest.json`, two `board-N.mjpg` streams, two `board-N.jsonl` metadata files, and a final `summary.json`. The manifest maps filenames to board IDs. Each metadata line records the JPEG byte offset and length, sequence, sensor and mapped timestamps, clock validity, cumulative delivery drops, reported exposure/gain, gain lock, and view geometry. JPEGs are concatenated directly without a video container; playback timing comes from the metadata. Recording quality defaults to 90 and can be changed with `--recording-quality`; `--preview-fps` controls only preview. Progress is printed every five seconds, and `/status` includes recording counts, sequence gaps, output directory, and faults. A recording error is reported and terminates the application.

Recording starts immediately when requested and includes frames during synchronization and gain warmup. Inspect `/status` for `ready`, phase metrics, and faults, and use frame metadata to select usable intervals. The two streams are independent board packets; match their sensor timestamps for this single-node case. The dual-board fake-backend checks validate remote controls, JPEG output, metadata, recording restart, and shutdown. Sustained 40 fps recording, disk throughput, phase quality, and optical alignment still require measurements on the actual Pi 5 and boards; use [validate_device.py](../tools/validate_device.py) for the separate device workflow.
