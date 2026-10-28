# Pi 5 Python examples

For setup from a blank card, follow the [fresh Pi walkthrough](../docs/pi-from-scratch.md), starting with Raspberry Pi Imager and SSH username/password configuration.

These six standalone files use application-owned libcamera capture with either local OpenCV windows or an HTTP web page. Each B0267 board supplies four views in one grayscale strip. All examples use a 5120×800, 8-bit sensor mode, a 1280×200 preview, 40 fps, 10 ms exposure, and analogue gain 1.0. Change those constants in the script when adapting the application.

| Hardware | OpenCV windows | HTTP web preview | Synchronization |
| --- | --- | --- | --- |
| One Pi 5, one B0267 board | [`single_board.py`](single_board.py) | [`single_board_http.py`](single_board_http.py) | None: these scripts do not import this library |
| One Pi 5, two B0267 boards | [`opencv_sync.py`](opencv_sync.py) | [`opencv_sync_http.py`](opencv_sync_http.py) | `SyncSession` with one reusable `CameraTiming` adapter per board |
| Two Pi 5 nodes, one or two B0267 boards each | [`two_nodes_sync.py`](two_nodes_sync.py) | [`two_nodes_sync_http.py`](two_nodes_sync_http.py) | One group of detected boards with network clock estimation; [setup below](#two-pi-5-nodes-with-one-or-two-boards-each) |

Complete [installation](../docs/installation.md#python) with the compatible vendor camera stack. The OpenCV-window scripts below need a graphical session on the Pi; the HTTP alternatives later in this guide need no Pi display. Start the single-board window preview without arguments:

```sh
.venv/bin/python examples/single_board.py
```

Start synchronized preview with two boards:

```sh
.venv/bin/python examples/opencv_sync.py
```

The scripts enumerate libcamera devices, sort their IDs, and select the first one or two. They print the selected IDs and, for dual-board capture, the reference board. Enumeration does not identify board models; these examples expect the selected devices to be B0267 boards. Missing devices produce an error. No JSON configuration or additional example files are needed. Press q or Esc in an OpenCV window, or Ctrl+C in the terminal, to exit.

To inspect IDs or choose specific boards, use:

```sh
.venv/bin/python examples/single_board.py --list
.venv/bin/python examples/single_board.py 'CAMERA_ID'
.venv/bin/python examples/opencv_sync.py 'REFERENCE_CAMERA_ID' 'SECOND_CAMERA_ID'
```

All six scripts support `--list`, which uses the camera SDK without opening cameras or importing OpenCV/NumPy. It can run in a plain SSH terminal. The synchronization scripts also require the installed synchronization core. Explicit IDs preserve their supplied order; use them when additional cameras are attached or the physical reference board must stay fixed. The two-node examples can run `--list` without network arguments or clock executables.

The dual-board example constructs its single-node configuration in `sync_config()` and uses the first board as the phase reference. The application submits requests and reports sensor metadata through `CameraTiming`; this library chooses frame-period corrections automatically. The capture thread continues during synchronization shutdown so the adapter can confirm baseline restoration before cameras close.

The terminal reports synchronization state, readiness, phase, p95, and maximum error once per second. Preview is available during startup and continues if synchronization stops or faults; a visible image alone does not establish synchronization. Readiness uses sensor timestamps and does not replace optical validation. See [the first-run guide](../docs/getting-started.md) for interpretation.

## HTTP web preview

Choose either command below on the Pi, including in a plain SSH session; no Pi graphical desktop is needed:

```sh
.venv/bin/python examples/single_board_http.py
# Or, with two boards:
.venv/bin/python examples/opencv_sync_http.py
```

Open `http://PI_IP:8000/` in a browser on a computer that can reach the Pi, replacing `PI_IP` with its existing address. The scripts listen on `0.0.0.0:8000` by default; `--bind` and `--port` change the listening address and port. Camera ID arguments and `--list` work as in the OpenCV examples. Each HTTP example is independent and needs no other example file.

The page refreshes latest JPEG snapshots at approximately 10 fps while acquisition remains configured at 40 fps. The dual-board page also shows synchronization state and phase metrics. HTTP requests read encoded snapshots and never access camera requests or control synchronization. Closing a tab leaves capture running; use Ctrl+C in the Pi terminal to stop the application. OpenCV is used only to encode JPEGs, with no `imshow` or `waitKey` calls; no Web framework or extra Python dependency is required.

Recording, SSH desktop preview, and managed capture applications live in [`tools/`](../tools/README.md). Configuration templates for those tools and multi-node deployments live in [`docs/configs/`](../docs/configs). Synthetic acquisition fixtures and CMake installation checks live in `tests/`.

## Two Pi 5 nodes with one or two boards each

Run the same standalone script on each Pi, selecting its identity with `--node`. Both Pis must have the compatible B0267 camera stack, the same B-CamSync core build, and the same example revision. Neither script imports another example or requires the managed add-on or a JSON configuration file.

Board counts are automatic. Each Pi selects up to two local camera IDs and exchanges its detected count with the other Pi before opening cameras. Both then build the same synchronization group from those counts: 1+1, 1+2, 2+1, or 2+2 boards. Missing second boards have no placeholder image or synchronization unit, and there is no board-count argument. Each node still needs at least one camera. The startup exchange briefly uses TCP 58080 on node-a and closes before the core starts; no additional port configuration is needed. Membership is fixed for that run; restart both applications after changing the connected boards.

Connect both Pis to an existing, mutually reachable IPv4 network; use wired Ethernet for this setup. Provide both nodes' addresses identically on both command lines. This example uses `network_mode="existing"`, so it does not change interface addresses, discover nodes through multicast, or require NetworkManager link setup. Use stable addresses for the duration of a run, such as DHCP reservations. Camera IDs are local to each Pi and can be identical on different Pis; only logical board IDs are shared across the group.

Install chrony on both Pis:

```sh
sudo apt update
sudo apt install chrony
command -v chronyd
command -v chronyc
```

`clock_mode="network"` uses the core's private chronyd processes to estimate clock mappings between the Pis. The core manages these processes and does not use them to adjust the system clock. You do not need to edit the system chrony configuration. Allow the nodes to communicate over TCP 58080 for synchronization control and UDP 58081 for private clock estimation, and allow your computer to reach each Pi's HTTP port 8000 for browser preview. Network clock quality must satisfy the configured bound before the group can become ready; working SSH or visible pictures alone does not establish clock or phase quality.

The following addresses are examples; replace them with your actual addresses. Start in either order, in a terminal on each Pi:

```sh
# On Pi A:
.venv/bin/python examples/two_nodes_sync_http.py \
  --node node-a --address-a 192.168.0.105 --address-b 192.168.0.106

# On Pi B:
.venv/bin/python examples/two_nodes_sync_http.py \
  --node node-b --address-a 192.168.0.105 --address-b 192.168.0.106
```

Each process enumerates local cameras and selects up to the first two sorted IDs. It prints their mapping to `board-a1` and, if present, `board-a2` on Pi A; Pi B uses `board-b1` and, if present, `board-b2`. All detected boards share the reference `board-a1`: the first selected board on Pi A. The first board on Pi B is another follower, rather than a second reference. If each Pi has just one board, the commands above automatically create a two-board group.

To select local devices explicitly, append one or two IDs from that Pi's `--list` output, preserving quotes around each ID:

```sh
.venv/bin/python examples/two_nodes_sync_http.py --list
.venv/bin/python examples/two_nodes_sync_http.py \
  --node node-a --address-a 192.168.0.105 --address-b 192.168.0.106 \
  'FIRST_LOCAL_CAMERA_ID' 'SECOND_LOCAL_CAMERA_ID'
```

Open `http://192.168.0.105:8000/` and `http://192.168.0.106:8000/` on your computer. Each page shows only that Pi's detected strips, with a compact node/group status above the images and collapsed group timing details below. Images stay local to their capture application; synchronization traffic carries timing and controls, rather than video. Both Pis may use HTTP port 8000 because they have different IP addresses. `--bind` and `--port` select the local HTTP listener as in the single-Pi examples. Refresh the whole page when switching between examples in an existing browser tab.

For OpenCV windows on each Pi's graphical desktop, replace `two_nodes_sync_http.py` with `two_nodes_sync.py` and keep the same network and optional camera arguments. No remote image relay or combined four-board browser page is provided by these minimal examples.

Before capture, each script prints its detected local count and waits for the other node's inventory; Ctrl+C cancels that wait. Once counts agree, preview starts while synchronization joins the group and estimates clocks. The terminal reports waiting nodes, clock validity/uncertainty, global readiness, and phase/p95/max for the detected logical boards. Wait for `ready=True`; default phase acceptance targets remain p95 ≤ 0.5 ms and max ≤ 1 ms across two settled windows. These two-node demos explicitly allow 0.2 ms (200 µs) of clock mapping uncertainty in both clock validation and phase estimation; the core's default remains 0.1 ms. This is a demo acceptance budget, not an improvement in measured clock precision. A later sample exceeding 200 µs still ends synchronization and requires an explicit restart. These are acceptance thresholds, not measured results from a two-Pi experiment. The example checks use simulated devices; two physical Pis and their connected B0267 boards still require a joint device run and separate optical validation.

Press Ctrl+C in an HTTP terminal, or q/Esc in an OpenCV window, to stop that local application. Stopping either node ends synchronization throughout the joined group and restores baseline frame periods. The other node continues its local preview, reports the group stop or fault, and marks readiness false. A disconnected peer also ends synchronization after communication failure detection. Close both applications and start both explicitly for a new round; the examples do not automatically restart synchronization. Closing a browser tab does not stop capture.
