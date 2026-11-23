"""A controllable libcamera request pipeline for the Python preview examples."""

from collections import deque
import os
import selectors
import tempfile
import threading
import time
from types import SimpleNamespace

from bcamsync.adapters import boot_time_ns


class Request:
    status = "complete"

    def __init__(self, cookie):
        self.cookie = cookie
        self.buffers = {}
        self.reuse()

    def add_buffer(self, stream, buffer):
        self.buffers[stream] = buffer
        return 0

    def reuse(self):
        self.controls = {}
        self.metadata = {}

    def set_control(self, control, value):
        self.controls[control] = value


class Camera:
    def __init__(self, manager, camera_id, storage):
        self.manager, self.id = manager, camera_id
        self.state = "available"
        self.stream = SimpleNamespace(stream=object(), stride=8, size=None)
        self.buffers = [
            SimpleNamespace(planes=[SimpleNamespace(fd=storage.fileno(), offset=0)],
                            metadata=SimpleNamespace(status="success", sequence=0))
            for _ in range(2)
        ]
        self.controls = {"FrameDurationLimits": SimpleNamespace(min=10_000, max=100_000)}
        self.pending = deque()
        self.sequence = 0
        self.timestamp = None
        self.applied_duration = None

    def _transition(self, previous, state, event):
        with self.manager.changed:
            assert self.state == previous, (self.id, event, self.state)
            self.state = state
            self.manager.record(event, self.id)
        return 0

    def acquire(self):
        return self._transition("available", "acquired", "acquire")

    def generate_configuration(self, _roles):
        return SimpleNamespace(at=lambda _: self.stream, validate=lambda: "valid")

    def configure(self, _configuration):
        assert all(camera.state != "running" for camera in self.manager.cameras.values())
        return self._transition("acquired", "configured", "configure")

    def allocate(self, stream):
        assert stream is self.stream.stream
        return self._transition("configured", "allocated", "allocate")

    def create_request(self, cookie):
        assert self.state == "allocated"
        assert all(camera.state != "running" for camera in self.manager.cameras.values())
        return Request(cookie)

    def start(self, controls):
        self.applied_duration = controls["FrameDurationLimits"][0]
        return self._transition("allocated", "running", "start")

    def queue_request(self, request):
        with self.manager.changed:
            assert self.state == "running"
            assert request not in self.pending and len(self.pending) < len(self.buffers)
            self.pending.append(request)
            self.manager.record("queue", self.id, request.cookie, request.controls["FrameDurationLimits"][0])
        return 0

    def stop(self):
        with self.manager.changed:
            self._transition("running", "allocated", "stop")
            self.pending.clear()

    def release(self):
        with self.manager.changed:
            assert self.state in {"acquired", "configured", "allocated"}
            self._transition(self.state, "available", "release")


class ScriptedLibcamera:
    """Release completions on demand, with a one-frame delay in reported controls."""

    def __init__(self, stack, camera_ids):
        self.changed = threading.Condition()
        self.events = deque(maxlen=80)
        self.completed = deque()
        self.delivered = self.handled = self.drained = 0
        self.event_fd, self.notify_fd = os.pipe()
        stack.callback(os.close, self.event_fd)
        stack.callback(os.close, self.notify_fd)
        storage = stack.enter_context(tempfile.TemporaryFile())
        storage.write(bytes(16))
        storage.flush()
        self.cameras = {name: Camera(self, name, storage) for name in camera_ids}
        self.sdk = SimpleNamespace(
            CameraManager=SimpleNamespace(singleton=lambda: self),
            StreamRole=SimpleNamespace(Viewfinder="preview"), Size=lambda *_: SimpleNamespace(width=4, height=2),
            PixelFormat=str, SensorConfiguration=SimpleNamespace,
            CameraConfiguration=SimpleNamespace(Status=SimpleNamespace(Invalid="invalid")),
            FrameBufferAllocator=lambda camera: SimpleNamespace(
                allocate=camera.allocate, buffers=lambda _: camera.buffers),
            Request=SimpleNamespace(Status=SimpleNamespace(Complete="complete")),
            FrameMetadata=SimpleNamespace(Status=SimpleNamespace(Success="success")),
            controls=SimpleNamespace(**{name: name for name in (
                "FrameDurationLimits", "AeEnable", "ExposureTimeMode", "AnalogueGainMode",
                "ExposureTime", "AnalogueGain", "SensorTimestamp", "FrameDuration")}))
        self.selectors = SimpleNamespace(DefaultSelector=self.selector, EVENT_READ=selectors.EVENT_READ)

    def record(self, event, *details):
        self.events.append((time.monotonic_ns(), event, *details))
        self.changed.notify_all()

    def get(self, camera_id):
        return self.cameras.get(camera_id)

    def selector(self):
        selector = selectors.DefaultSelector()
        select = selector.select

        def mark_drained(timeout):
            # Capture reaches select again after observing metadata and requeueing.
            with self.changed:
                self.drained = self.handled
                self.changed.notify_all()
            return select(timeout)

        selector.select = mark_drained
        return selector

    def get_ready_requests(self):
        with self.changed:
            os.read(self.event_fd, len(self.completed))
            requests = list(self.completed)
            self.completed.clear()
            self.handled = self.delivered
            return requests

    def wait_for_requests(self):
        with self.changed:
            if not self.changed.wait_for(
                    lambda: all(len(camera.pending) == len(camera.buffers)
                                for camera in self.cameras.values()), timeout=3):
                raise AssertionError("Cameras did not queue all initial requests")

    def complete(self, camera_id, phase_ns=0):
        """Release one queued completion and wait for the application to process it."""
        camera = self.cameras[camera_id]
        with self.changed:
            assert camera.pending, (camera_id, "no request to complete")
            request = camera.pending.popleft()
            requested = request.controls["FrameDurationLimits"][0]
            reported = camera.applied_duration
            now = boot_time_ns()
            if camera.timestamp is None:
                period_ns = reported * 1000
                timestamp = (now // period_ns + 1) * period_ns + phase_ns
            else:
                timestamp = camera.timestamp + reported * 1000
        # Advance the sensor at frame cadence. Only the scenario releases completions.
        time.sleep(max(0, timestamp - boot_time_ns()) / 1e9)
        with self.changed:
            assert camera.state == "running", (camera_id, "camera closed before completion")
            camera.timestamp = timestamp
            camera.applied_duration = requested
            camera.sequence += 1
            request.buffers[camera.stream.stream].metadata.sequence = camera.sequence
            request.metadata = {"SensorTimestamp": timestamp, "FrameDuration": reported}
            self.completed.append(request)
            self.delivered += 1
            self.record("complete", camera.id, request.cookie, camera.sequence, requested, reported)
            os.write(self.notify_fd, b"\x01")
            if not self.changed.wait_for(
                    lambda: self.drained == self.delivered or camera.state != "running", timeout=2):
                raise AssertionError(f"Application did not process a completion for {camera_id}")
        return reported
