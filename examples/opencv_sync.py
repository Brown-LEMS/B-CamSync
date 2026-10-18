"""Test two Pi 5 boards with application-owned capture and SyncSession.

libcamera supplies sensor timestamps and per-request frame-period controls.
OpenCV windows, loopback browser preview, and MJPG output are optional.
The default run measures synchronization only.
"""

import argparse
from contextlib import ExitStack
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import mmap
from pathlib import Path
import selectors
import threading
import time
from urllib.parse import unquote, urlsplit

from bcamsync import SyncConfig, SyncSession
from bcamsync.adapters.libcamera import CameraTiming


class BrowserPreview:
    """Loopback-only HTTP snapshots; JPEG encoding never runs on capture or HTTP threads."""

    def __init__(self, capture, port=8765, fps=10):
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError("preview fps must be finite and positive")
        self.capture, self.fps = capture, fps
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.images = {}
        self.status = b'{"state":"starting","ready":false}'
        self.error = None
        preview = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(2)

            def log_message(self, *_):
                pass

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/":
                    code, data, content_type = 200, preview.page(), "text/html; charset=utf-8"
                elif path == "/status":
                    with preview.lock:
                        data = preview.status
                    code, content_type = 200, "application/json"
                elif path.startswith("/preview/") and path.endswith(".jpg"):
                    unit = unquote(path[len("/preview/"):-len(".jpg")])
                    if unit not in capture.units:
                        code, data, content_type = 404, b"Unknown board", "text/plain"
                    else:
                        with preview.lock:
                            latest = preview.images.get(unit)
                        available = latest is not None and time.monotonic() - latest[0] < 1
                        code, data = (200, latest[1]) if available else (204, b"")
                        content_type = "image/jpeg"
                else:
                    code, data, content_type = 404, b"Unknown route", "text/plain"
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (ConnectionError, TimeoutError):
                    pass  # A browser disconnect does not own acquisition.

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.http_thread = threading.Thread(target=self.server.serve_forever,
                                            kwargs={"poll_interval": 0.1}, name="preview-http")
        self.encoder_thread = threading.Thread(target=self.encode, name="preview-jpeg")

    def start(self):
        self.http_thread.start()
        self.encoder_thread.start()
        print(f"Browser preview: http://127.0.0.1:{self.server.server_port}", flush=True)

    def publish_status(self, status):
        encoded = json.dumps(status, allow_nan=False).encode()
        with self.lock:
            self.status = encoded
        self.check()

    def check(self):
        if self.error is not None:
            raise RuntimeError(f"Preview encoding failed: {self.error}")

    def encode(self):
        encoded_at = {}
        try:
            while not self.done.is_set():
                with self.capture.image_lock:
                    images = dict(self.capture.images)
                for unit, (timestamp, image) in images.items():
                    if timestamp == encoded_at.get(unit) or time.monotonic() - timestamp >= 1:
                        continue
                    ok, jpeg = self.capture.cv2.imencode(
                        ".jpg", image, [self.capture.cv2.IMWRITE_JPEG_QUALITY, 80])
                    if not ok:
                        raise RuntimeError(f"JPEG encoding failed for {unit}")
                    with self.lock:
                        self.images[unit] = timestamp, jpeg.tobytes()
                    encoded_at[unit] = timestamp
                self.done.wait(1 / self.fps)
        except Exception as error:
            self.error = error

    def page(self):
        boards = "".join(f'<section><h2>{escape(unit)}</h2><img data-unit="{escape(unit)}" hidden></section>'
                         for unit in self.capture.units)
        interval_ms = max(1, round(1000 / self.fps))
        return ("""<!doctype html><html><head><meta charset="utf-8"><title>B-CamSync preview</title>
<style>body{font:16px system-ui;margin:2rem}img{max-width:100%}pre{white-space:pre-wrap}</style></head>
<body><h1>Two-board preview</h1><p>Closing this page leaves capture running. Use Ctrl+C in the Pi application to stop.</p>
<pre id="status">Starting…</pre>""" + boards + """
<script>
function clearImage(img){clearTimeout(img.expires);img.hidden=true;img.removeAttribute('src');if(img.dataset.url){URL.revokeObjectURL(img.dataset.url);delete img.dataset.url}}
async function image(img){try{const r=await fetch('/preview/'+encodeURIComponent(img.dataset.unit)+'.jpg',{cache:'no-store'});if(r.status===200){const url=URL.createObjectURL(await r.blob());clearImage(img);img.dataset.url=url;img.src=url;img.hidden=false;img.expires=setTimeout(()=>clearImage(img),1000)}else{clearImage(img)}}catch{clearImage(img)}finally{setTimeout(()=>image(img),""" + str(interval_ms) + """)}}
async function status(){try{const r=await fetch('/status',{cache:'no-store'});document.querySelector('#status').textContent=JSON.stringify(await r.json(),null,2)}catch{document.querySelector('#status').textContent='Preview unavailable'}finally{setTimeout(status,1000)}}
status();document.querySelectorAll('img').forEach(image);
</script></body></html>""").encode()

    def close(self):
        self.done.set()
        if self.http_thread.ident is not None:
            self.server.shutdown()
            self.http_thread.join()
        self.server.server_close()
        if self.encoder_thread.ident is not None:
            self.encoder_thread.join()


class Capture:
    """The example owns cameras, request buffers, and the acquisition thread."""

    def __init__(self, config, stack, display, *, http_preview=False):
        import cv2
        import libcamera
        self.cv2, self.sdk = cv2, libcamera
        self.manager = libcamera.CameraManager.singleton()
        self.requests, self.submitted, self.units = {}, {}, {}
        self.allocators, self.configurations = [], []
        self.closed = threading.Event()
        self.display = display
        self.http_preview = http_preview
        self.images = {}
        self.writers = {}
        self.image_lock = threading.Lock()
        self.thread = None
        baseline = 1_000_000 // config["fps"]
        for unit in config["units"]:
            camera = self.manager.get(unit["camera_id"])
            if camera is None:
                raise RuntimeError(f"Camera not found: {unit['camera_id']}")
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
            if camera.configure(configuration):
                raise RuntimeError(f"Cannot configure camera: {camera.id}")
            self.configurations.append(configuration)
            limit = camera.controls[libcamera.controls.FrameDurationLimits]
            timing = CameraTiming(unit["id"], config["fps"], limit.min, limit.max)
            self.units[unit["id"]] = timing
            allocator = libcamera.FrameBufferAllocator(camera)
            if allocator.allocate(stream.stream) < 0:
                raise RuntimeError(f"Cannot allocate buffers: {camera.id}")
            self.allocators.append(allocator)
            controls = {libcamera.controls.FrameDurationLimits: (baseline, baseline),
                        libcamera.controls.AeEnable: False,
                        libcamera.controls.ExposureTimeMode: 1,
                        libcamera.controls.AnalogueGainMode: 1,
                        libcamera.controls.ExposureTime: 10000,
                        libcamera.controls.AnalogueGain: 1.0}
            camera.start(controls)
            stack.callback(camera.stop)
            for buffer in allocator.buffers(stream.stream):
                request = camera.create_request(len(self.requests))
                request.add_buffer(stream.stream, buffer)
                self.requests[request.cookie] = (camera, stream, timing, request)
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
        self.thread = threading.Thread(target=self.run, name="external-capture")
        self.thread.start()

    def start_recording(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        with self.image_lock:
            for _, stream, timing, _ in self.requests.values():
                if timing.unit_id in self.writers:
                    continue
                path = directory / f"{timing.unit_id}.avi"
                writer = self.cv2.VideoWriter(str(path), self.cv2.VideoWriter_fourcc(*"MJPG"),
                                              1_000_000 / timing.baseline_us,
                                              (stream.size.width, stream.size.height))
                if not writer.isOpened():
                    raise RuntimeError(f"Cannot open MJPG output: {path}")
                self.writers[timing.unit_id] = writer
                print(f"MJPG output: {path}", flush=True)

    def run(self):
        import numpy as np
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.manager.event_fd, selectors.EVENT_READ)
                while not self.closed.is_set():
                    if not selector.select(0.1):
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
                            if self.display or self.http_preview or self.writers:
                                with mmap.mmap(buffer.planes[0].fd, stream.frame_size,
                                               flags=mmap.MAP_SHARED, prot=mmap.PROT_READ) as pixels:
                                    yuv = np.frombuffer(pixels, dtype=np.uint8).reshape(
                                        stream.size.height * 3 // 2, stream.stride)
                                    image = self.cv2.cvtColor(yuv, self.cv2.COLOR_YUV2BGR_I420)
                                    del yuv
                                with self.image_lock:
                                    if self.display or self.http_preview:
                                        self.images[timing.unit_id] = time.monotonic(), image
                                    writer = self.writers.get(timing.unit_id)
                                    if writer is not None:
                                        writer.write(image)
                        request.reuse()
                        self.queue(request)
        except Exception as error:
            for timing in self.units.values():
                timing.error = str(error)
            print(f"Capture failed: {error}", flush=True)

    def stop(self):
        self.closed.set()
        if self.thread is not None:
            self.thread.join()
        for writer in self.writers.values():
            writer.release()


def status_record(status, captures):
    units = []
    for state in status.units:
        timing = captures[state.unit_id]
        frames = timing.snapshot().frames
        fps = ((len(frames) - 1) * 1e9 / (frames[-1].sensor_timestamp_ns - frames[0].sensor_timestamp_ns)
               if len(frames) > 1 else 0)
        units.append({"unit_id": state.unit_id, "phase_ms": state.phase_ms,
                      "p95_ms": state.p95_ms, "max_ms": state.max_ms,
                      "fps": fps, "corrections": timing.corrections, "fault": state.fault})
    return {"state": status.state, "ready": status.ready, "fault": status.fault, "units": units}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--display", action="store_true", help="optional local OpenCV windows")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--video-dir", type=Path, help="optional downstream MJPG AVI output after readiness")
    parser.add_argument("--http-preview", action="store_true", help="serve browser preview on Pi loopback")
    parser.add_argument("--http-port", type=int, default=8765)
    parser.add_argument("--preview-fps", type=float, default=10)
    parser.add_argument("--continuous", action="store_true", help="run one session until Ctrl+C; requires --repeat 1")
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0 or args.repeat < 1:
        parser.error("duration and repeat must be positive")
    if args.video_dir is not None and args.repeat != 1:
        parser.error("video output requires --repeat 1")
    if args.continuous and args.repeat != 1:
        parser.error("continuous mode requires --repeat 1")
    if not math.isfinite(args.preview_fps) or args.preview_fps <= 0 or not 1 <= args.http_port <= 65535:
        parser.error("preview fps must be finite and positive; HTTP port must be in 1..65535")
    document = json.loads(args.config.read_text())
    sync = SyncConfig.from_file(args.config)
    if len(sync.nodes) != 1 or len(sync.units) != 2:
        parser.error("this example requires one Pi and two boards")
    samples = []
    result = {"measurement_kind": "sensor_timestamp_phase_control",
              "config": document, "sync_config": json.loads(sync.to_json()),
              "seconds_per_run": args.seconds, "requested_runs": args.repeat,
              "passed": False, "runs": [], "samples": samples}
    with ExitStack() as stack:
        capture = Capture(document, stack, args.display, http_preview=args.http_preview)
        capture.start()
        preview = None
        try:
            if args.http_preview:
                preview = BrowserPreview(capture, args.http_port, args.preview_fps)
                preview.start()
            time.sleep(1)
            with SyncSession(sync, sync.nodes[0].id, capture.units) as session:
                for run in range(args.repeat):
                    startup = time.monotonic()
                    if run:
                        session.start()
                    deadline = time.monotonic() + sync.startup_timeout_seconds
                    while not session.wait_ready(1):
                        sample = status_record(session.status(), capture.units)
                        if preview is not None:
                            preview.publish_status(sample)
                        print(json.dumps(sample), flush=True)
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Synchronization readiness timed out")
                    began = time.monotonic()
                    if args.video_dir is not None:
                        capture.start_recording(args.video_dir)
                    run_result = {"run": run + 1, "startup_seconds": began - startup}
                    result["runs"].append(run_result)
                    while args.continuous or time.monotonic() - began < args.seconds:
                        sample = status_record(session.status(), capture.units)
                        sample.update(run=run + 1, elapsed_seconds=time.monotonic() - began)
                        if not args.continuous:
                            samples.append(sample)
                        if preview is not None:
                            preview.publish_status(sample)
                        print(json.dumps(sample), flush=True)
                        if sample["fault"] or not sample["ready"]:
                            raise RuntimeError(sample["fault"] or "Synchronization readiness lost")
                        if args.display:
                            with capture.image_lock:
                                for unit, (_, image) in capture.images.items():
                                    capture.cv2.imshow(unit, image)
                            if capture.cv2.waitKey(1) == 27:
                                return
                        time.sleep(1)
                    before = {unit: value.snapshot().frames[-1].camera_sequence
                              for unit, value in capture.units.items()}
                    session.stop()
                    stopped = session.status()
                    if stopped.state != "stopped" or stopped.fault:
                        raise RuntimeError(stopped.fault or "Synchronization did not stop cleanly")
                    time.sleep(0.2)
                    for unit, value in capture.units.items():
                        if value.snapshot().frames[-1].camera_sequence <= before[unit]:
                            raise RuntimeError("External capture stopped with synchronization")
                    print(f"Run {run + 1}: sync stopped; external capture continues", flush=True)
                    run_result["capture_continued_after_sync_stop"] = True
                result["passed"] = True
        finally:
            try:
                if preview is not None:
                    preview.close()
            finally:
                capture.stop()
                if args.display:
                    capture.cv2.destroyAllWindows()
                if args.output:
                    args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped by user", flush=True)
