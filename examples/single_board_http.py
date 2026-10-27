"""Preview one B0267 board over HTTP without a Pi display or synchronization."""

import argparse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mmap
import selectors
import threading
import time


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("camera_id", nargs="?", help="board ID; defaults to the first ID in sorted order")
    parser.add_argument("--list", action="store_true", help="list camera IDs without starting capture")
    parser.add_argument("--bind", default="0.0.0.0", help="HTTP listening address")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be in 1..65535")

    import libcamera

    manager = libcamera.CameraManager.singleton()
    if args.list or args.camera_id is None:
        cameras = sorted(manager.cameras, key=lambda camera: camera.id)
        if args.list:
            for camera in cameras:
                print(camera.id)
            return
        if not cameras:
            parser.error("no libcamera cameras found")
        camera = cameras[0]
    else:
        camera = manager.get(args.camera_id)
    if camera is None:
        parser.error(f"Camera not found: {args.camera_id}")
    print(f"Board: {camera.id}", flush=True)

    # Finish image imports before cameras can start producing frames.
    import cv2
    import numpy as np

    with ExitStack() as stack:
        preview = WebPreview(args.bind, args.port, ("board-1",))
        stack.callback(preview.close)
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
        allocator = libcamera.FrameBufferAllocator(camera)
        if allocator.allocate(stream.stream) < 0:
            raise RuntimeError("Cannot allocate camera buffers")

        # Each board supplies a strip containing its four camera views.
        # Exposure and frame period are fixed here; no SyncSession is needed.
        period_us = 1_000_000 // 40
        controls = {libcamera.controls.FrameDurationLimits: (period_us, period_us),
                    libcamera.controls.AeEnable: False,
                    libcamera.controls.ExposureTimeMode: 1,
                    libcamera.controls.AnalogueGainMode: 1,
                    libcamera.controls.ExposureTime: 10000,
                    libcamera.controls.AnalogueGain: 1.0}
        if camera.start(controls):
            raise RuntimeError("Cannot start camera")
        stack.callback(camera.stop)
        requests = []
        for buffer in allocator.buffers(stream.stream):
            request = camera.create_request()
            if request.add_buffer(stream.stream, buffer):
                raise RuntimeError("Cannot attach camera buffer")
            if camera.queue_request(request):
                raise RuntimeError("Cannot queue camera request")
            requests.append(request)

        preview.start()
        print("Capturing one board; use Ctrl+C to exit.", flush=True)
        next_preview = 0
        with selectors.DefaultSelector() as selector:
            selector.register(manager.event_fd, selectors.EVENT_READ)
            try:
                while True:
                    if selector.select(0.05):
                        for request in manager.get_ready_requests():
                            if request.status != libcamera.Request.Status.Complete:
                                raise RuntimeError("Camera request was cancelled")
                            buffer = request.buffers[stream.stream]
                            now = time.monotonic()
                            if buffer.metadata.status == libcamera.FrameMetadata.Status.Success and now >= next_preview:
                                plane = buffer.planes[0]
                                offset = plane.offset % mmap.PAGESIZE
                                with mmap.mmap(plane.fd, offset + stream.stride * stream.size.height,
                                               flags=mmap.MAP_SHARED, prot=mmap.PROT_READ,
                                               offset=plane.offset - offset) as pixels:
                                    image = np.ndarray((stream.size.height, stream.stride),
                                                       dtype=np.uint8, buffer=pixels, offset=offset)
                                    image = image[:, :stream.size.width].copy()
                                preview.publish({"board-1": image}, {"state": "capturing"})
                                next_preview = now + 0.1
                            request.reuse()
                            if camera.queue_request(request):
                                raise RuntimeError("Cannot queue camera request")
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
