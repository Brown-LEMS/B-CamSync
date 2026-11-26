"""Exercise cold acquisition and sustained tracking through the real public session in Docker."""

import argparse
import bisect
from collections import deque
from contextlib import ExitStack
import math
from statistics import mean
import threading
import time

from bcamsync import SyncSession, boot_time_ns
from bcamsync.adapters.libcamera import CameraTiming
from synthetic_capture import local_config


class QuantizedCamera:
    """A free-running sensor with separate request queue and register application delays."""

    def __init__(self, name, fps, first_ns, drift_ppm, quantum_us, offset_us, lag):
        self.timing = CameraTiming(name, fps, 22221, 199993)
        self.initial = first_ns
        self.period = (self.timing.baseline_us - 18) * 1000 * (1 + drift_ppm * 1e-6)
        self.quantum, self.offset, self.lag = quantum_us, offset_us, lag
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.capture)
        self.frames = []
        self.error = None

    def capture(self):
        try:
            pending = deque(self.timing.next_submission() for _ in range(8))
            register = deque([0] * self.lag)
            sensor = self.initial - self.period
            while not self.done.is_set():
                tag = pending.popleft()
                delta = tag.duration_us - self.timing.baseline_us
                register.append(delta)
                movement = self.quantum * math.floor((register.popleft() + self.offset) / self.quantum)
                sensor += self.period + 1000 * movement
                seq = len(self.frames)
                # Simulated physical exposure and the read-only timing measurement are distinct.
                physical_ns = round(sensor)
                sensor_ns = 1000 * round(sensor / 1000 + 1.5 * math.sin(seq * 1.25))
                if self.done.wait(max(0, (physical_ns + 2_000_000 - boot_time_ns()) * 1e-9)):
                    return
                self.timing.observe(seq, sensor_ns, round(self.timing.baseline_us - 10 + movement), tag)
                self.frames.append((physical_ns, delta))
                pending.append(self.timing.next_submission())
        except Exception as exc:
            self.error = exc

    def stop(self):
        self.done.set()
        self.thread.join()
        if self.error:
            raise self.error


def verify(fps):
    # None of these sensor mode parameters or initial offsets are passed to the controller.
    quantum, offset, lag, initial_fraction, drift = {
        24: (24.5, 16.2, 3, .32, -4),
        30: (18.5, 7.2, 4, -.37, 8),
        40: (24.5, 12.1, 4, .44, -7),
        44: (24.5, 12.3, 3, -.42, 6),
    }[fps]
    first = boot_time_ns() + 100_000_000
    reference = QuantizedCamera("reference", fps, first, 0, quantum, offset, lag)
    follower = QuantizedCamera("follower", fps, first + round(initial_fraction * 1e9 / fps),
                               drift, quantum, offset, lag)
    config = local_config()
    config.fps = fps
    config.startup_timeout_seconds = 90
    with ExitStack() as cleanup:
        for camera in (reference, follower):
            camera.thread.start()
            cleanup.callback(camera.stop)
        session = SyncSession(config, "local", {"reference": reference.timing, "follower": follower.timing})
        session.start()
        cleanup.callback(session.stop)
        start = time.monotonic()
        while not session.wait_ready(2):
            print(f"{fps} Hz cold startup: {time.monotonic()-start:.1f}s, {session.status().state}", flush=True)
            if time.monotonic() - start > 95:
                raise AssertionError("Cold phase acquisition timed out")
        ready = boot_time_ns()
        print(f"{fps} Hz acquired in {time.monotonic()-start:.1f}s", flush=True)
        for _ in range(3):
            time.sleep(4)
            status = session.status()
            if not status.ready or status.fault:
                raise AssertionError(f"Lost tracking: {status.fault}")
    # Establish correspondence once, then compare simulated exposure time at that fixed offset.
    ref = [t for t, _ in reference.frames]
    peer = [t for t, _ in follower.frames]
    i = bisect.bisect_left(ref, ready)
    j = bisect.bisect_left(peer, ref[i])
    offset = min((j-1, j), key=lambda n: abs(peer[n]-ref[i])) - i
    lo, hi = bisect.bisect_left(ref, ready + 4_000_000_000), bisect.bisect_left(ref, ready + 11_000_000_000)
    errors = sorted(abs(peer[k+offset] - ref[k]) / 1000 for k in range(lo, hi))
    p99 = errors[math.ceil(.99 * len(errors)) - 1]
    print(f"{fps} Hz: {len(errors)} pairs, physical MAE {mean(errors):.2f}, P99 {p99:.2f} us", flush=True)
    if len(errors) < 6 * fps or p99 > 50:
        raise AssertionError(f"Insufficient sustained physical alignment at {fps} Hz")
    if any(delta for _, delta in reference.frames):
        raise AssertionError("The reference sensor received a duration correction")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fps", type=int, choices=(24, 30, 40, 44))
    verify(parser.parse_args().fps)
