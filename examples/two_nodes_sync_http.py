"""Synchronize B0267 boards across two Pi 5 nodes; preview only local cameras over HTTP."""

import argparse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mmap
import selectors
import shutil
import socket
import threading
import time

from bcamsync import NodeConfig, SyncConfig, SyncSession, SyncUnitConfig
from bcamsync.adapters.libcamera import CameraTiming


def sync_config(address_a, address_b, boards_a=2, boards_b=2):
    """Use both nodes' detected counts; camera IDs stay local to each Pi."""
    if boards_a not in (1, 2) or boards_b not in (1, 2):
        raise ValueError("Each node must have one or two boards")
    config = SyncConfig()
    config.group_id = "pi5-two-nodes"
    config.fps = 40
    config.network_mode = "existing"
    config.clock_mode = "network"
    # Demo clock budget: 200 us. Keep the default phase readiness thresholds.
    config.clock_max_offset_ms = 0.2
    policy = config.phase_policy
    policy.max_mapping_span_ns = 200_000
    config.phase_policy = policy
    nodes, units = [], []
    for node_id, address, prefix, count in (("node-a", address_a, "a", boards_a),
                                            ("node-b", address_b, "b", boards_b)):
        node = NodeConfig()
        node.id, node.address = node_id, address
        nodes.append(node)
        for index in range(1, count + 1):
            unit = SyncUnitConfig()
            unit.id, unit.node_id = f"board-{prefix}{index}", node_id
            units.append(unit)
    config.nodes, config.units = nodes, units
    config.reference_unit = "board-a1"
    config.validate()
    return config


def discover_boards(local_node, address_a, address_b, local_count):
    """Exchange detected counts before SyncSession fixes the group's membership.

    Node A briefly uses its synchronization TCP port for this startup exchange.
    Both scripts close that connection before starting the synchronization core.
    """
    if local_node == "node-a":
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((address_a, 58080))
            server.listen(1)
            server.settimeout(1)
            while True:
                print("Waiting for node-b to report its local boards...", flush=True)
                try:
                    connection, _ = server.accept()
                    break
                except TimeoutError:
                    continue
            with connection:
                connection.settimeout(5)
                with connection.makefile("rwb") as stream:
                    peer = json.loads(stream.readline(1024))
                    if peer.get("node") != "node-b" or peer.get("boards") not in (1, 2):
                        raise ValueError("Invalid node-b board inventory")
                    counts = {"node-a": local_count, "node-b": peer["boards"]}
                    stream.write((json.dumps(counts) + "\n").encode())
                    stream.flush()
                    if stream.readline(1024) != b"ready\n":
                        raise ValueError("Node-b did not confirm the board inventory")
    else:
        while True:
            print("Waiting for node-a to exchange local board counts...", flush=True)
            try:
                connection = socket.create_connection((address_a, 58080), timeout=1,
                                                      source_address=(address_b, 0))
                break
            except (ConnectionRefusedError, TimeoutError):
                time.sleep(1)
        with connection:
            connection.settimeout(5)
            with connection.makefile("rwb") as stream:
                stream.write((json.dumps({"node": "node-b", "boards": local_count}) + "\n").encode())
                stream.flush()
                counts = json.loads(stream.readline(1024))
                if counts.get("node-a") not in (1, 2) or counts.get("node-b") != local_count:
                    raise ValueError("Invalid shared board inventory")
                stream.write(b"ready\n")
                stream.flush()
    print(f"Detected group: node-a={counts['node-a']} board(s), "
          f"node-b={counts['node-b']} board(s)", flush=True)
    return counts["node-a"], counts["node-b"]


class WebPreview:
    """Serve latest JPEG snapshots and status; browser requests never touch cameras."""

    def __init__(self, address, port, local_node, boards):
        self.lock = threading.Lock()
        self.images = {}
        self.status = json.dumps({"local_node": local_node, "state": "starting",
                                  "ready": False, "fault": "", "waiting_nodes": []}).encode()
        preview = self
        routes = {f"/{board}.jpg": board for board in boards}
        images = "".join(f'<h2>{board}</h2><img data-src="/{board}.jpg" hidden>' for board in boards)
        page = ("""<!doctype html><meta charset="utf-8"><title>B0267 preview</title>
<style>body{font:16px system-ui;margin:2rem}img{max-width:100%}</style>
<h1>B0267 local preview</h1><p>This page shows only this Pi's detected boards. Use Ctrl+C on the Pi to stop capture.</p>
<p id="summary">Starting...</p>""" + images + """
<details><summary>Group timing status (all detected boards)</summary><pre id="status">Starting...</pre></details>
<script>
async function refreshFrame(img) {
  try {
    const response = await fetch(img.dataset.src, {cache: 'no-store'});
    if (response.status === 200) {
      const old = img.dataset.url;
      img.src = img.dataset.url = URL.createObjectURL(await response.blob());
      img.hidden = false;
      if (old) URL.revokeObjectURL(old);
    } else { img.hidden = true; }
  } catch { img.hidden = true; }
  finally { setTimeout(() => refreshFrame(img), 100); }
}
async function refreshStatus() {
  try {
    const response = await fetch('/status', {cache: 'no-store'});
    const status = await response.json();
    document.querySelector('#summary').textContent = `${status.local_node}: ${status.state}; ready=${status.ready}; waiting=${status.waiting_nodes.join(", ")}; ${status.fault}`;
    document.querySelector('#status').textContent = JSON.stringify(status, null, 2);
  } catch {
    document.querySelector('#summary').textContent = 'Preview unavailable';
    document.querySelector('#status').textContent = 'Preview unavailable';
  }
  finally { setTimeout(refreshStatus, 1000); }
}
document.querySelectorAll('img').forEach(refreshFrame);
refreshStatus();
</script>""").encode()

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(2)

            def log_message(self, *_):
                pass

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                with preview.lock:
                    if path == "/":
                        code, data, kind = 200, page, "text/html; charset=utf-8"
                    elif path == "/status":
                        code, data, kind = 200, preview.status, "application/json"
                    elif path in routes:
                        data = preview.images.get(routes[path], b"")
                        code, kind = (200 if data else 204), "image/jpeg"
                    else:
                        code, data, kind = 404, b"Not found", "text/plain"
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (ConnectionError, TimeoutError):
                    pass  # Closing a browser does not own capture.

        self.server = ThreadingHTTPServer((address, port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.1}, name="web-preview")

    def start(self):
        self.thread.start()
        address, port = self.server.server_address
        print(f"HTTP preview listening on {address}:{port}; open http://PI_IP:{port}/", flush=True)

    def publish(self, images, status):
        import cv2
        encoded = {}
        for board, image in images.items():
            ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ok:
                raise RuntimeError(f"JPEG encoding failed: {board}")
            encoded[board] = jpeg.tobytes()
        data = json.dumps(status, allow_nan=False).encode()
        with self.lock:
            self.images, self.status = encoded, data

    def close(self):
        if self.thread.ident is not None:
            self.server.shutdown()
            self.thread.join()
        self.server.server_close()


class Capture:
    """Application capture; only timing and frame-period controls enter this library."""

    def __init__(self, camera_ids, config, local_node, stack):
        import libcamera
        self.sdk = libcamera
        self.manager = libcamera.CameraManager.singleton()
        self.units, self.requests, self.submitted = {}, {}, {}
        self.allocators, self.configurations = [], []
        self.images = {}
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.error = None
        self.thread = None
        local_units = [unit for unit in config.units if unit.node_id == local_node]
        for unit, camera_id in zip(local_units, camera_ids):
            camera = self.manager.get(camera_id)
            if camera is None:
                raise RuntimeError(f"Camera not found: {camera_id}")
            if camera.acquire():
                raise RuntimeError(f"Cannot acquire camera: {camera.id}")
            stack.callback(camera.release)
            configuration = camera.generate_configuration([libcamera.StreamRole.Viewfinder])
            stream = configuration.at(0)
            stream.size = libcamera.Size(1280, 200)
            stream.pixel_format = libcamera.PixelFormat("YUV420")
            stream.buffer_count = 8
            sensor = libcamera.SensorConfiguration()
            sensor.output_size = libcamera.Size(5120, 800)
            sensor.bit_depth = 8
            configuration.sensor_config = sensor
            if configuration.validate() == libcamera.CameraConfiguration.Status.Invalid:
                raise RuntimeError("Invalid capture configuration")
            if str(stream.pixel_format) != "YUV420":
                raise RuntimeError("The preview requires YUV420")
            if camera.configure(configuration):
                raise RuntimeError(f"Cannot configure camera: {camera.id}")
            self.configurations.append(configuration)
            limit = camera.controls[libcamera.controls.FrameDurationLimits]
            timing = CameraTiming(unit.id, config.fps, max(limit.min, 10000), limit.max)
            self.units[unit.id] = timing
            allocator = libcamera.FrameBufferAllocator(camera)
            if allocator.allocate(stream.stream) < 0:
                raise RuntimeError("Cannot allocate camera buffers")
            self.allocators.append(allocator)
            controls = {libcamera.controls.FrameDurationLimits: (timing.baseline_us, timing.baseline_us),
                        libcamera.controls.AeEnable: False,
                        libcamera.controls.ExposureTimeMode: 1,
                        libcamera.controls.AnalogueGainMode: 1,
                        libcamera.controls.ExposureTime: 10000,
                        libcamera.controls.AnalogueGain: 1.0}
            if camera.start(controls):
                raise RuntimeError(f"Cannot start camera: {camera.id}")
            stack.callback(camera.stop)
            for buffer in allocator.buffers(stream.stream):
                request = camera.create_request(len(self.requests))
                if request.add_buffer(stream.stream, buffer):
                    raise RuntimeError("Cannot attach camera buffer")
                self.requests[request.cookie] = camera, stream, timing, request
                self.queue(request)

    def queue(self, request):
        camera, _, timing, _ = self.requests[request.cookie]
        submitted = timing.next_submission()
        request.set_control(self.sdk.controls.FrameDurationLimits,
                            (submitted.duration_us, submitted.duration_us))
        self.submitted[request.cookie] = submitted
        if camera.queue_request(request):
            raise RuntimeError(f"Cannot queue camera request: {camera.id}")

    def start(self):
        self.thread = threading.Thread(target=self.run, name="capture")
        self.thread.start()

    def run(self):
        import numpy as np
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.manager.event_fd, selectors.EVENT_READ)
                while not self.done.is_set():
                    if not selector.select(0.05):
                        continue
                    for request in self.manager.get_ready_requests():
                        if request.status != self.sdk.Request.Status.Complete:
                            raise RuntimeError("Camera request was cancelled")
                        _, stream, timing, _ = self.requests[request.cookie]
                        buffer = request.buffers[stream.stream]
                        if buffer.metadata.status == self.sdk.FrameMetadata.Status.Success:
                            metadata = request.metadata
                            timing.observe(buffer.metadata.sequence,
                                           metadata[self.sdk.controls.SensorTimestamp],
                                           metadata[self.sdk.controls.FrameDuration],
                                           self.submitted[request.cookie])
                            plane = buffer.planes[0]
                            offset = plane.offset % mmap.PAGESIZE
                            with mmap.mmap(plane.fd, offset + stream.stride * stream.size.height,
                                           flags=mmap.MAP_SHARED, prot=mmap.PROT_READ,
                                           offset=plane.offset - offset) as pixels:
                                image = np.ndarray((stream.size.height, stream.stride),
                                                   dtype=np.uint8, buffer=pixels, offset=offset)
                                image = image[:, :stream.size.width].copy()
                            with self.lock:
                                self.images[timing.unit_id] = image
                        request.reuse()
                        self.queue(request)
        except Exception as error:
            self.error = error
            for timing in self.units.values():
                timing.error = str(error)

    def close(self):
        self.done.set()
        if self.thread is not None:
            self.thread.join()


def status_record(status, clock, local_node):
    """Global synchronization status, separate from the local preview images."""
    return {"local_node": local_node, "state": status.state, "ready": status.ready,
            "fault": status.fault, "waiting_nodes": list(status.waiting_nodes),
            "stop_node": status.stop_node, "stop_reason": status.stop_reason,
            "clock": {"valid": clock.valid, "uncertainty_ns": clock.uncertainty_ns},
            "units": [{"id": unit.unit_id, "state": unit.state, "ready": unit.ready,
                       "phase_ms": unit.phase_ms, "p95_ms": unit.p95_ms,
                       "max_ms": unit.max_ms, "fault": unit.fault} for unit in status.units]}


def report(status, clock, local_node):
    print(f"{local_node}: sync={status.state}, ready={status.ready}, fault={status.fault}, "
          f"waiting={status.waiting_nodes}, clock_valid={clock.valid}, "
          f"clock_uncertainty={clock.uncertainty_ns / 1000:.1f} us", flush=True)
    for unit in status.units:
        print(f"  {unit.unit_id}: phase={unit.phase_ms:.3f} ms, "
              f"p95={unit.p95_ms:.3f} ms, max={unit.max_ms:.3f} ms", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("camera_ids", nargs="*", metavar="CAMERA_ID",
                        help="one or two local IDs; defaults to up to two sorted camera IDs")
    parser.add_argument("--list", action="store_true", help="list local camera IDs without network setup")
    parser.add_argument("--node", choices=("node-a", "node-b"), help="identity of this Pi")
    parser.add_argument("--address-a", help="node-a's existing IPv4 address (same on both Pis)")
    parser.add_argument("--address-b", help="node-b's existing IPv4 address (same on both Pis)")
    parser.add_argument("--bind", default="0.0.0.0", help="HTTP listening address")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.list:
        import libcamera
        for camera_id in sorted(camera.id for camera in libcamera.CameraManager.singleton().cameras):
            print(camera_id)
        return
    if not all((args.node, args.address_a, args.address_b)):
        parser.error("--node, --address-a and --address-b are required unless --list is used")
    if not 1 <= args.port <= 65535:
        parser.error("port must be in 1..65535")
    if len(args.camera_ids) > 2:
        parser.error("provide at most two local camera IDs")
    if len(set(args.camera_ids)) != len(args.camera_ids):
        parser.error("local camera IDs must be distinct")
    try:
        config = sync_config(args.address_a, args.address_b)
    except ValueError as error:
        parser.error(str(error))
    if any(shutil.which(command) is None for command in ("chronyd", "chronyc")):
        parser.error("chronyd and chronyc are required; install chrony on both Pis")
    if not args.camera_ids:
        import libcamera
        camera_ids = sorted(camera.id for camera in libcamera.CameraManager.singleton().cameras)
        if not camera_ids:
            parser.error("no local cameras found")
        args.camera_ids = camera_ids[:2]
    print(f"Detected {len(args.camera_ids)} local board(s).", flush=True)
    try:
        boards_a, boards_b = discover_boards(args.node, args.address_a, args.address_b, len(args.camera_ids))
    except KeyboardInterrupt:
        return
    config = sync_config(args.address_a, args.address_b, boards_a, boards_b)
    local_units = [unit for unit in config.units if unit.node_id == args.node]
    print(f"Local node: {args.node}; group reference: {config.reference_unit} (node-a)", flush=True)
    for unit, camera_id in zip(local_units, args.camera_ids):
        print(f"  {unit.id}: {camera_id}", flush=True)
    # Finish image imports before cameras can start producing frames.
    import cv2
    import numpy

    with ExitStack() as stack:
        preview = WebPreview(args.bind, args.port, args.node, tuple(unit.id for unit in local_units))
        stack.callback(preview.close)
        capture = Capture(args.camera_ids, config, args.node, stack)
        stack.callback(capture.close)
        capture.start()
        session = SyncSession(config, args.node, capture.units)
        try:
            session.start()
            preview.start()
            print("Capturing local boards; use Ctrl+C to exit.", flush=True)
            next_report = 0
            while True:
                if capture.error is not None:
                    raise capture.error
                status = session.status()
                clock = session.clock()
                with capture.lock:
                    images = dict(capture.images)
                preview.publish(images, status_record(status, clock, args.node))
                if time.monotonic() >= next_report:
                    report(status, clock, args.node)
                    next_report = time.monotonic() + 1
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass
        finally:
            # Completion callbacks must keep running until baseline restoration finishes.
            session.stop()


if __name__ == "__main__":
    main()
