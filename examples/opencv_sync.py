"""Preview two B0267 boards on one Pi 5 with application-owned capture and SyncSession."""

import argparse
from contextlib import ExitStack
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
        self.ready = threading.Event()
        self.cameras = []
        self.stops = ExitStack()
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
            requests = []
            self.cameras.append((camera, controls, requests))
            for buffer in allocator.buffers(stream.stream):
                request = camera.create_request(len(self.requests))
                if request.add_buffer(stream.stream, buffer):
                    raise RuntimeError("Cannot attach camera buffer")
                self.requests[request.cookie] = camera, stream, timing, request
                requests.append(request)

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
        if not self.ready.wait(10):
            with self.lock:
                missing = sorted(set(self.units) - self.images.keys())
            raise RuntimeError(f"Timed out waiting for first camera frames: {missing}")
        if self.error is not None:
            raise self.error

    def run(self):
        try:
            import numpy as np
            with selectors.DefaultSelector() as selector:
                selector.register(self.manager.event_fd, selectors.EVENT_READ)
                # All cameras and requests are prepared before any camera starts.
                for camera, controls, requests in self.cameras:
                    if camera.start(controls):
                        raise RuntimeError(f"Cannot start camera: {camera.id}")
                    self.stops.callback(camera.stop)
                    for request in requests:
                        self.queue(request)
                while not self.done.is_set():
                    if not selector.select(0.05):
                        continue
                    for request in self.manager.get_ready_requests():
                        camera, stream, timing, _ = self.requests[request.cookie]
                        if request.status != self.sdk.Request.Status.Complete:
                            raise RuntimeError(f"Camera request was cancelled: {timing.unit_id} ({camera.id})")
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
                        if len(self.images) == len(self.units):
                            self.ready.set()
        except Exception as error:
            self.error = error
            for timing in self.units.values():
                timing.error = str(error)
            self.ready.set()

    def close(self):
        self.done.set()
        if self.thread is not None:
            self.thread.join()
        self.stops.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("camera_ids", nargs="*", metavar="CAMERA_ID",
                        help="two board IDs; defaults to the first two IDs in sorted order")
    parser.add_argument("--list", action="store_true", help="list camera IDs without starting capture")
    args = parser.parse_args()
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
        capture = Capture(args.camera_ids, config, stack)
        stack.callback(capture.close)
        stack.callback(cv2.destroyAllWindows)
        capture.start()
        session = SyncSession(config, config.nodes[0].id, capture.units)
        try:
            session.start()
            print("Previewing both boards; press q or Esc in a window, or Ctrl+C, to exit.")
            next_report = 0
            while True:
                if capture.error is not None:
                    raise capture.error
                status = session.status()
                with capture.lock:
                    images = dict(capture.images)
                for unit, image in images.items():
                    cv2.imshow(unit, image)
                if time.monotonic() >= next_report:
                    print(f"sync={status.state}, ready={status.ready}, fault={status.fault}", flush=True)
                    for unit in status.units:
                        print(f"  {unit.unit_id}: phase={unit.phase_ms:.3f} ms, "
                              f"p95={unit.p95_ms:.3f} ms, max={unit.max_ms:.3f} ms", flush=True)
                    next_report = time.monotonic() + 1
                if cv2.waitKey(10) & 0xff in (ord("q"), 27):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            # Completion callbacks must keep running until baseline restoration finishes.
            session.stop()


if __name__ == "__main__":
    main()
