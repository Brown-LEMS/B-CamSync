"""Test two Pi 5 boards with application-owned capture and SyncSession.

libcamera supplies sensor timestamps and per-request frame-period controls.
OpenCV display and MJPG output are optional; the default measures synchronization only.
"""

import argparse
from collections import deque
from contextlib import ExitStack
import json
import math
import mmap
from pathlib import Path
import selectors
import threading
import time

from bcamsync import FrameTiming, PhaseActuator, SyncConfig, SyncSession, SyncUnit, TimingSnapshot
from bcamsync import boot_time_ns, monotonic_ns


class CameraTiming(SyncUnit):
    def __init__(self, unit_id, fps, minimum_us, maximum_us):
        super().__init__()
        self.unit_id = unit_id
        self.baseline_us = 1_000_000 // fps
        self.minimum_us, self.maximum_us = minimum_us, maximum_us
        self.actuator = PhaseActuator()
        self.frames = deque(maxlen=240)
        self.lock = threading.Lock()
        self.binding = None
        self.error = ""
        self.restoration = None
        self.corrections = 0

    def bind(self, session_id, round_id):
        with self.lock:
            self.actuator.bind(session_id, round_id)
            self.binding = session_id, round_id

    def next_submission(self):
        with self.lock:
            return self.actuator.next(self.baseline_us, monotonic_ns())

    def observe(self, sequence, sensor_ns, reported_us, submitted):
        with self.lock:
            frame = FrameTiming()
            frame.camera_sequence = sequence
            frame.sensor_timestamp_ns = frame.timestamp_ns = sensor_ns
            frame.requested_duration_us = submitted.duration_us
            frame.reported_duration_us = reported_us
            frame.correction_id = submitted.correction_id
            self.frames.append(frame)
            if submitted.restoration:
                self.restoration = submitted
            if self.restoration is not None and abs(reported_us - self.baseline_us) <= 50:
                self.actuator.completed(self.restoration, sequence, sensor_ns)
                self.restoration = None

    def snapshot(self):
        with self.lock:
            result = TimingSnapshot()
            result.unit_id = self.unit_id
            result.snapshot_timestamp_ns = boot_time_ns()
            result.frames = list(self.frames)
            result.correction = self.actuator.snapshot()
            return result

    def adjust(self, pulse):
        with self.lock:
            result = self.actuator.adjust(*self.binding, pulse, boot_time_ns(), monotonic_ns(),
                                          self.baseline_us, self.minimum_us, self.maximum_us)
            self.corrections += 1
            print(f"{self.unit_id}: correction {pulse.correction_id}, "
                  f"delta={pulse.delta_us}us, frames={pulse.count}", flush=True)
            return result

    def cancel(self):
        with self.lock:
            self.actuator.cancel(*self.binding)

    def fault(self):
        return self.error


class Capture:
    """The example owns cameras, request buffers, and the acquisition thread."""

    def __init__(self, config, stack, display):
        import cv2
        import libcamera
        self.cv2, self.sdk = cv2, libcamera
        self.manager = libcamera.CameraManager.singleton()
        self.requests, self.submitted, self.units = {}, {}, {}
        self.allocators, self.configurations = [], []
        self.closed = threading.Event()
        self.display = display
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
                            if self.display or self.writers:
                                with mmap.mmap(buffer.planes[0].fd, stream.frame_size,
                                               flags=mmap.MAP_SHARED, prot=mmap.PROT_READ) as pixels:
                                    yuv = np.frombuffer(pixels, dtype=np.uint8).reshape(
                                        stream.size.height * 3 // 2, stream.stride)
                                    image = self.cv2.cvtColor(yuv, self.cv2.COLOR_YUV2BGR_I420)
                                    del yuv
                                with self.image_lock:
                                    if self.display:
                                        self.images[timing.unit_id] = image
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
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0 or args.repeat < 1:
        parser.error("duration and repeat must be positive")
    if args.video_dir is not None and args.repeat != 1:
        parser.error("video output requires --repeat 1")
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
        capture = Capture(document, stack, args.display)
        capture.start()
        try:
            time.sleep(1)
            with SyncSession(sync, sync.nodes[0].id, capture.units) as session:
                for run in range(args.repeat):
                    startup = time.monotonic()
                    if run:
                        session.start()
                    deadline = time.monotonic() + sync.startup_timeout_seconds
                    while not session.wait_ready(1):
                        print(json.dumps(status_record(session.status(), capture.units)), flush=True)
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Synchronization readiness timed out")
                    began = time.monotonic()
                    if args.video_dir is not None:
                        capture.start_recording(args.video_dir)
                    run_result = {"run": run + 1, "startup_seconds": began - startup}
                    result["runs"].append(run_result)
                    while time.monotonic() - began < args.seconds:
                        sample = status_record(session.status(), capture.units)
                        sample.update(run=run + 1, elapsed_seconds=time.monotonic() - began)
                        samples.append(sample)
                        print(json.dumps(sample), flush=True)
                        if sample["fault"] or not sample["ready"]:
                            raise RuntimeError(sample["fault"] or "Synchronization readiness lost")
                        if args.display:
                            with capture.image_lock:
                                for unit, image in capture.images.items():
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
            capture.stop()
            if args.display:
                capture.cv2.destroyAllWindows()
            if args.output:
                args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
