"""Preview two B0267 boards on one Pi 5 with application-owned capture and SyncSession."""

import argparse
from contextlib import ExitStack
from itertools import count
import json
import mmap
import os
import selectors
import threading
import time

from bcamsync import NodeConfig, SyncConfig, SyncSession, SyncUnitConfig, boot_time_ns
from bcamsync.adapters.libcamera import CameraTiming


STARTUP_RETRIES = 20
STARTUP_FRAMES = 3
ATTEMPT_SECONDS = 2.0


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


class CameraStream:
    """Own a board's requests and the evidence for its current startup attempt."""

    def __init__(self, camera, controls, stream, timing, buffers, verbose=False):
        self.camera, self.controls, self.stream, self.timing = camera, controls, stream, timing
        self.buffers = buffers
        self.verbose = verbose
        self.requests = {}
        self.running = False
        self.attempt = 0

    def prepare(self, cookies):
        self.requests.clear()
        for buffer in self.buffers:
            request = self.camera.create_request(next(cookies))
            if request.add_buffer(self.stream.stream, buffer):
                raise RuntimeError("Cannot attach camera buffer")
            self.requests[request.cookie] = request

    def start(self):
        self.started = time.monotonic()
        self.attempt += 1
        if self.attempt == 1:
            self.began = self.started
        self.window_started = self.started
        self.valid_frames = self.streak = 0
        self.expected = self.first = self.last = self.qualified = None
        self.observation = None
        if self.camera.start(self.controls):
            raise RuntimeError(f"Cannot start camera: {self.camera.id}")
        self.running = True

    def stop(self):
        if self.running:
            self.camera.stop()
            self.running = False

    def break_streak(self):
        if self.qualified is not None:
            self.window_started = time.monotonic()
        self.expected = self.qualified = None
        self.streak = 0

    def observe_startup(self, observation):
        now = time.monotonic()
        sequence = observation[0]
        self.valid_frames += 1
        if self.first is None:
            self.first = now
        if sequence != self.expected:
            self.break_streak()
        self.streak += 1
        self.expected = (sequence + 1) % (1 << 32)
        self.last = now
        self.observation = observation
        if self.streak == STARTUP_FRAMES:
            self.qualified = now
            self.qualified_count = self.valid_frames

    def report(self, outcome, reason):
        if not self.verbose:
            if outcome == "retry":
                print(f"{self.timing.unit_id}: retry {self.attempt}/{STARTUP_RETRIES}", flush=True)
            elif outcome == "ready":
                print(f"{self.timing.unit_id}: camera ready (attempt {self.attempt})", flush=True)
            return
        now = self.qualified if outcome == "ready" else time.monotonic()
        print("B-CamSync startup " + json.dumps({
            "unit": self.timing.unit_id, "camera_id": self.camera.id, "attempt": self.attempt,
            "outcome": outcome, "reason": reason,
            "valid_frames": self.qualified_count if outcome == "ready" else self.valid_frames,
            "first_frame_ms": None if self.first is None else round((self.first - self.started) * 1000, 2),
            "elapsed_ms": round((now - self.started) * 1000, 2),
            "total_ms": round((now - self.began) * 1000, 2)}), flush=True)


class Capture:
    """Application capture; only timing and frame-period controls enter this library."""

    def __init__(self, camera_ids, config, stack, *, verbose=False):
        import libcamera
        self.sdk = libcamera
        self.manager = libcamera.CameraManager.singleton()
        self.units, self.owners, self.submitted = {}, {}, {}
        self.cookies = count()
        self.allocators, self.configurations = [], []
        self.images = {}
        self.image_timing = {}
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
            board = CameraStream(camera, controls, stream, timing, allocator.buffers(stream.stream), verbose)
            self.cameras.append(board)
            self.stops.callback(board.stop)
            self.prepare(board)

    def prepare(self, board):
        board.prepare(self.cookies)
        self.owners.update(dict.fromkeys(board.requests, board))

    def queue(self, board, request):
        camera, timing = board.camera, board.timing
        submitted = timing.next_submission()
        request.set_control(self.sdk.controls.FrameDurationLimits,
                            (submitted.duration_us, submitted.duration_us))
        self.submitted[request.cookie] = submitted
        if camera.queue_request(request):
            raise RuntimeError(f"Cannot queue camera request: {camera.id}")

    def start(self, np):
        """Start both boards; pass None to collect timing without copying pixels."""
        self.thread = threading.Thread(target=self.run, args=(np,), name="capture")
        self.thread.start()
        # Each board's attempts have their own deadline inside the capture thread.
        if not self.ready.wait((ATTEMPT_SECONDS + 1) * (STARTUP_RETRIES + 1) * len(self.cameras)):
            raise RuntimeError("Camera startup deadline expired")
        if self.error is not None:
            raise self.error

    def start_board(self, board):
        try:
            board.start()
            for request in board.requests.values():
                self.queue(board, request)
        except Exception as error:
            board.report("error", str(error))
            raise

    def retry(self, board, reason):
        exhausted = board.attempt == STARTUP_RETRIES + 1
        board.report("exhausted" if exhausted else "retry", reason)
        if exhausted:
            raise RuntimeError(f"Camera startup exhausted: {board.timing.unit_id} ({board.camera.id}), {reason}")
        board.stop()
        for cookie in board.requests:
            del self.owners[cookie]
            self.submitted.pop(cookie, None)
        with self.lock:
            self.images.pop(board.timing.unit_id, None)
            self.image_timing.pop(board.timing.unit_id, None)
        self.prepare(board)
        self.start_board(board)

    def run(self, np):
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.manager.event_fd, selectors.EVENT_READ)
                # All cameras and requests are prepared before any camera starts.
                for board in self.cameras:
                    self.start_board(board)
                starting = True
                while not self.done.is_set():
                    failures = {}
                    if selector.select(0.05):
                        for request in self.manager.get_ready_requests():
                            board = self.owners.get(request.cookie)
                            if board is None or board in failures:
                                continue
                            timing, stream = board.timing, board.stream
                            if request.status != self.sdk.Request.Status.Complete:
                                if not starting:
                                    raise RuntimeError(f"Camera request was cancelled: {timing.unit_id} ({board.camera.id})")
                                failures[board] = "cancelled"
                                continue
                            buffer = request.buffers[stream.stream]
                            if buffer.metadata.status == self.sdk.FrameMetadata.Status.Success:
                                metadata = request.metadata
                                observation = (buffer.metadata.sequence, metadata[self.sdk.controls.SensorTimestamp],
                                               metadata[self.sdk.controls.FrameDuration], self.submitted[request.cookie])
                                if starting:
                                    board.observe_startup(observation)
                                else:
                                    timing.observe(*observation)
                                if np is not None and (not starting or board.qualified is not None):
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
                                        self.image_timing[timing.unit_id] = observation[:2]
                            elif starting:
                                board.break_streak()
                            request.reuse()
                            self.queue(board, request)
                    if not starting:
                        continue
                    now = time.monotonic()
                    for board in self.cameras:
                        since = board.window_started if board.qualified is None else board.last
                        if now - since >= ATTEMPT_SECONDS:
                            failures.setdefault(board, "insufficient_frames" if board.valid_frames else "no_frames")
                    for board, reason in failures.items():
                        self.retry(board, reason)
                    if not all(board.qualified is not None for board in self.cameras):
                        continue
                    # A board can stall while its peer is restarting. Compare sensor
                    # timestamps before committing both generations to the sync session.
                    timestamps = [board.observation[1] for board in self.cameras]
                    if max(timestamps) - min(timestamps) > 2 * self.cameras[0].timing.baseline_us * 1000:
                        continue
                    for board in self.cameras:
                        board.timing.observe(*board.observation)
                        board.report("ready", "continuous_frames")
                    starting = False
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


class PreviewStart:
    """Select latest preview images by their exposure, even if delivery is delayed."""

    def __init__(self, mode):
        self.mode = mode
        self.sequences = None
        self.ready_ns = None

    def images(self, capture, status):
        if self.mode == "aligned" and self.sequences is None:
            if status.state not in ("aligned", "tracking"):
                return {}
            self.sequences = {u.unit_id: u.aligned_from_sequence for u in status.units}
        if self.mode == "ready" and self.ready_ns is None:
            if not status.ready:
                return {}
            # On this Pi SensorTimestamp and boot_time_ns() use CLOCK_BOOTTIME.
            self.ready_ns = boot_time_ns()
        with capture.lock:
            if self.sequences is not None:
                return {unit: image for unit, image in capture.images.items()
                        if self.sequences[unit] is not None
                        and capture.image_timing[unit][0] >= self.sequences[unit]}
            if self.ready_ns is not None:
                return {unit: image for unit, image in capture.images.items()
                        if capture.image_timing[unit][1] >= self.ready_ns}
            return dict(capture.images)


def main(*, cli=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("camera_ids", nargs="*", metavar="CAMERA_ID",
                        help="two board IDs; defaults to the first two IDs in sorted order")
    parser.add_argument("--list", action="store_true", help="list camera IDs without starting capture")
    parser.add_argument("--start-at", choices=("first", "aligned", "ready"), default="first",
                        help="show the first available images, aligned images, or images exposed after readiness")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show camera diagnostics and synchronization statistics")
    args = parser.parse_args()
    try:
        run_preview(args, parser)
    except (RuntimeError, OSError) as error:
        if not cli or args.verbose:
            raise
        parser.exit(1, f"Camera preview failed: {error} (use -v for details)\n")


def run_preview(args, parser):
    if len(args.camera_ids) not in (0, 2):
        parser.error("provide either no camera IDs or two distinct camera IDs")
    if len(args.camera_ids) == 2 and args.camera_ids[0] == args.camera_ids[1]:
        parser.error("the two boards must have distinct camera IDs")
    if not args.verbose and not os.environ.get("LIBCAMERA_LOG_FILE"):
        os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "*:FATAL")
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
    import numpy as np

    config = sync_config()
    with ExitStack() as stack:
        capture = Capture(args.camera_ids, config, stack, verbose=args.verbose)
        stack.callback(capture.close)
        stack.callback(cv2.destroyAllWindows)
        capture.start(np)
        session = SyncSession(config, config.nodes[0].id, capture.units)
        try:
            session.start()
            preview_start = PreviewStart(args.start_at)
            print(f"Preview start: {args.start_at}. Press q or Esc in a window, or Ctrl+C, to exit.")
            next_report = 0
            last_sync = None
            while True:
                if capture.error is not None:
                    raise capture.error
                status = session.status()
                for unit, image in preview_start.images(capture, status).items():
                    cv2.imshow(unit, image)
                sync = (status.state, status.ready, status.fault)
                if not args.verbose and sync != last_sync:
                    if status.fault:
                        print(f"Synchronization failed: {status.fault}", flush=True)
                    elif status.ready:
                        print("Synchronization ready", flush=True)
                    elif status.state == "aligned":
                        print("Synchronization aligned; checking frame timing quality", flush=True)
                    else:
                        print(f"Synchronization {status.state}", flush=True)
                    last_sync = sync
                if args.verbose and time.monotonic() >= next_report:
                    print(f"sync={status.state}, ready={status.ready}, fault={status.fault}", flush=True)
                    for unit in status.units:
                        print(f"  {unit.unit_id}: phase={unit.phase_ms:.3f} ms, "
                              f"p95={unit.p95_ms:.3f} ms, max={unit.max_ms:.3f} ms, "
                              f"aligned_from_sequence={unit.aligned_from_sequence}", flush=True)
                    next_report = time.monotonic() + 1
                if cv2.waitKey(10) & 0xff in (ord("q"), 27):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            # Completion callbacks must keep running until baseline restoration finishes.
            session.stop()


if __name__ == "__main__":
    main(cli=True)
