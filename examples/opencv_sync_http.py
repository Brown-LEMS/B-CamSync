"""Synchronize two B0267 boards on one Pi 5 and preview over HTTP without a Pi display."""

import argparse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mmap
import selectors
import threading
import time

from bcamsync import NodeConfig, SyncConfig, SyncSession, SyncUnitConfig
from bcamsync.adapters.libcamera import CameraTiming


def sync_config():
    """Two boards share the Pi's local clock; the first is the phase reference."""
    config = SyncConfig()
    config.group_id = "pi5-local"
    config.fps = 40
    node = NodeConfig()
    node.id = "pi5"
    config.nodes = [node]
    units = []
    for name in ("board-1", "board-2"):
        unit = SyncUnitConfig()
        unit.id, unit.node_id = name, node.id
        units.append(unit)
    config.units = units
    config.reference_unit = units[0].id
    config.validate()
    return config


class WebPreview:
    """Serve latest JPEG snapshots and status; browser requests never touch cameras."""

    def __init__(self, address, port, boards):
        self.lock = threading.Lock()
        self.images = {}
        self.status = b'{"state":"starting"}'
        preview = self
        routes = {f"/{board}.jpg": board for board in boards}
        images = "".join(f'<h2>{board}</h2><img data-src="/{board}.jpg" hidden>' for board in boards)
        page = ("""<!doctype html><meta charset="utf-8"><title>B0267 preview</title>
<style>body{font:16px system-ui;margin:2rem}img{max-width:100%}</style>
<h1>B0267 preview</h1><p>Close this page freely; use Ctrl+C on the Pi to stop capture.</p>
<pre id="status">Starting...</pre>""" + images + """
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
    document.querySelector('#status').textContent = JSON.stringify(await response.json(), null, 2);
  } catch { document.querySelector('#status').textContent = 'Preview unavailable'; }
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

    def __init__(self, camera_ids, config, stack):
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
        for unit, camera_id in zip(config.units, camera_ids):
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("camera_ids", nargs="*", metavar="CAMERA_ID",
                        help="two board IDs; defaults to the first two IDs in sorted order")
    parser.add_argument("--list", action="store_true", help="list camera IDs without starting capture")
    parser.add_argument("--bind", default="0.0.0.0", help="HTTP listening address")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be in 1..65535")
    if len(args.camera_ids) not in (0, 2):
        parser.error("provide either no camera IDs or two distinct camera IDs")
    if len(args.camera_ids) == 2 and args.camera_ids[0] == args.camera_ids[1]:
        parser.error("the two boards must have distinct camera IDs")
    if args.list or not args.camera_ids:
        import libcamera
        camera_ids = sorted(camera.id for camera in libcamera.CameraManager.singleton().cameras)
        if args.list:
            for camera_id in camera_ids:
                print(camera_id)
            return
        if len(camera_ids) < 2:
            parser.error(f"two cameras are required; found {len(camera_ids)}")
        args.camera_ids = camera_ids[:2]
    print(f"board-1 (reference): {args.camera_ids[0]}\nboard-2: {args.camera_ids[1]}", flush=True)
    # Finish image imports before cameras can start producing frames.
    import cv2
    import numpy

    config = sync_config()
    with ExitStack() as stack:
        preview = WebPreview(args.bind, args.port, ("board-1", "board-2"))
        stack.callback(preview.close)
        capture = Capture(args.camera_ids, config, stack)
        stack.callback(capture.close)
        capture.start()
        session = SyncSession(config, config.nodes[0].id, capture.units)
        try:
            session.start()
            preview.start()
            print("Capturing both boards; use Ctrl+C to exit.", flush=True)
            next_report = 0
            while True:
                if capture.error is not None:
                    raise capture.error
                status = session.status()
                with capture.lock:
                    images = dict(capture.images)
                preview.publish(images, {"state": status.state, "ready": status.ready,
                                         "fault": status.fault,
                                         "units": [{"id": unit.unit_id, "phase_ms": unit.phase_ms,
                                                    "p95_ms": unit.p95_ms, "max_ms": unit.max_ms}
                                                   for unit in status.units]})
                if time.monotonic() >= next_report:
                    print(f"sync={status.state}, ready={status.ready}, fault={status.fault}", flush=True)
                    for unit in status.units:
                        print(f"  {unit.unit_id}: phase={unit.phase_ms:.3f} ms, "
                              f"p95={unit.p95_ms:.3f} ms, max={unit.max_ms:.3f} ms", flush=True)
                    next_report = time.monotonic() + 1
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass
        finally:
            # Completion callbacks must keep running until baseline restoration finishes.
            session.stop()


if __name__ == "__main__":
    main()
