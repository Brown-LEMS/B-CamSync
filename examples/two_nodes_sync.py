"""Synchronize four B0267 boards across two Pi 5 nodes; preview only local cameras in OpenCV windows."""

import argparse
from contextlib import ExitStack
import mmap
import selectors
import shutil
import threading
import time

from bcamsync import NodeConfig, SyncConfig, SyncSession, SyncUnitConfig
from bcamsync.adapters.libcamera import CameraTiming


def sync_config(address_a, address_b):
    """One shared four-board topology; only each node's camera IDs stay local."""
    config = SyncConfig()
    config.group_id = "pi5-four-boards"
    config.fps = 40
    config.network_mode = "existing"
    config.clock_mode = "network"
    nodes, units = [], []
    for node_id, address, prefix in (("node-a", address_a, "a"), ("node-b", address_b, "b")):
        node = NodeConfig()
        node.id, node.address = node_id, address
        nodes.append(node)
        for index in (1, 2):
            unit = SyncUnitConfig()
            unit.id, unit.node_id = f"board-{prefix}{index}", node_id
            units.append(unit)
    config.nodes, config.units = nodes, units
    config.reference_unit = "board-a1"
    config.validate()
    return config


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
                        help="two local board IDs; defaults to the first two sorted camera IDs")
    parser.add_argument("--list", action="store_true", help="list local camera IDs without network setup")
    parser.add_argument("--node", choices=("node-a", "node-b"), help="identity of this Pi")
    parser.add_argument("--address-a", help="node-a's existing IPv4 address (same on both Pis)")
    parser.add_argument("--address-b", help="node-b's existing IPv4 address (same on both Pis)")
    args = parser.parse_args()
    if args.list:
        import libcamera
        for camera_id in sorted(camera.id for camera in libcamera.CameraManager.singleton().cameras):
            print(camera_id)
        return
    if not all((args.node, args.address_a, args.address_b)):
        parser.error("--node, --address-a and --address-b are required unless --list is used")
    if len(args.camera_ids) not in (0, 2):
        parser.error("provide either no camera IDs or two distinct local camera IDs")
    if len(args.camera_ids) == 2 and args.camera_ids[0] == args.camera_ids[1]:
        parser.error("the two local boards must have distinct camera IDs")
    try:
        config = sync_config(args.address_a, args.address_b)
    except ValueError as error:
        parser.error(str(error))
    if any(shutil.which(command) is None for command in ("chronyd", "chronyc")):
        parser.error("chronyd and chronyc are required; install chrony on both Pis")
    if not args.camera_ids:
        import libcamera
        camera_ids = sorted(camera.id for camera in libcamera.CameraManager.singleton().cameras)
        if len(camera_ids) < 2:
            parser.error(f"two local cameras are required; found {len(camera_ids)}")
        args.camera_ids = camera_ids[:2]
    local_units = [unit for unit in config.units if unit.node_id == args.node]
    print(f"Local node: {args.node}; group reference: {config.reference_unit} (node-a)", flush=True)
    for unit, camera_id in zip(local_units, args.camera_ids):
        print(f"  {unit.id}: {camera_id}", flush=True)
    # Finish image imports before cameras can start producing frames.
    import cv2
    import numpy

    with ExitStack() as stack:
        capture = Capture(args.camera_ids, config, args.node, stack)
        stack.callback(capture.close)
        stack.callback(cv2.destroyAllWindows)
        capture.start()
        session = SyncSession(config, args.node, capture.units)
        try:
            session.start()
            print("Previewing both boards; press q or Esc in a window, or Ctrl+C, to exit.")
            next_report = 0
            while True:
                if capture.error is not None:
                    raise capture.error
                status = session.status()
                clock = session.clock()
                with capture.lock:
                    images = dict(capture.images)
                for unit, image in images.items():
                    cv2.imshow(unit, image)
                if time.monotonic() >= next_report:
                    report(status, clock, args.node)
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
