"""Preview one B0267 board on a Pi 5. One board needs no synchronization."""

import argparse
from contextlib import ExitStack
import json
import mmap
import os
import selectors
import time


STARTUP_RETRIES = 20
STARTUP_FRAMES = 3
ATTEMPT_SECONDS = 2.0


def main(*, cli=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("camera_id", nargs="?", help="board ID; defaults to the first ID in sorted order")
    parser.add_argument("--list", action="store_true", help="list camera IDs without starting capture")
    parser.add_argument("-v", "--verbose", action="store_true", help="show libcamera diagnostics and detailed startup events")
    args = parser.parse_args()
    try:
        run_preview(args, parser)
    except (RuntimeError, OSError) as error:
        if not cli or args.verbose:
            raise
        parser.exit(1, f"Camera preview failed: {error} (use -v for details)\n")


def run_preview(args, parser):
    if not args.verbose and not os.environ.get("LIBCAMERA_LOG_FILE"):
        os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "*:FATAL")
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

    import cv2
    import numpy as np

    with ExitStack() as stack:
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
        stack.callback(cv2.destroyAllWindows)
        print("Previewing one board; press q or Esc in the window, or Ctrl+C, to exit.", flush=True)
        began = time.monotonic()
        with selectors.DefaultSelector() as selector:
            selector.register(manager.event_fd, selectors.EVENT_READ)
            try:
                for attempt in range(1, STARTUP_RETRIES + 2):
                    requests = []
                    for buffer in allocator.buffers(stream.stream):
                        request = camera.create_request(attempt)
                        if request.add_buffer(stream.stream, buffer):
                            raise RuntimeError("Cannot attach camera buffer")
                        requests.append(request)

                    started = time.monotonic()
                    first = None
                    frames = streak = 0
                    previous = None
                    ready = False

                    def report(outcome, reason):
                        if not args.verbose:
                            if outcome == "retry":
                                print(f"board-1: retry {attempt}/{STARTUP_RETRIES}", flush=True)
                            elif outcome == "ready":
                                print(f"board-1: camera ready (attempt {attempt})", flush=True)
                            return
                        now = time.monotonic()
                        print("B-CamSync startup " + json.dumps({
                            "unit": "board-1", "camera_id": camera.id, "attempt": attempt,
                            "outcome": outcome, "reason": reason, "valid_frames": frames,
                            "first_frame_ms": None if first is None else round((first - started) * 1000, 2),
                            "elapsed_ms": round((now - started) * 1000, 2),
                            "total_ms": round((now - began) * 1000, 2)}), flush=True)

                    reason = ""
                    try:
                        if camera.start(controls):
                            raise RuntimeError("Cannot start camera")
                        try:
                            for request in requests:
                                if camera.queue_request(request):
                                    raise RuntimeError("Cannot queue camera request")
                            while not reason:
                                if selector.select(0.05):
                                    for request in manager.get_ready_requests():
                                        if request.cookie != attempt:
                                            continue  # A stopped generation can still have completions in the manager.
                                        if request.status != libcamera.Request.Status.Complete:
                                            if ready:
                                                raise RuntimeError("Camera request was cancelled")
                                            reason = "cancelled"
                                            break
                                        buffer = request.buffers[stream.stream]
                                        now = time.monotonic()
                                        if buffer.metadata.status == libcamera.FrameMetadata.Status.Success:
                                            if not ready:
                                                if first is None:
                                                    first = now
                                                frames += 1
                                                sequence = buffer.metadata.sequence
                                                streak = streak + 1 if sequence == previous else 1
                                                previous = (sequence + 1) % (1 << 32)
                                                if streak == STARTUP_FRAMES:
                                                    ready = True
                                                    report("ready", "continuous_frames")
                                            if ready:
                                                plane = buffer.planes[0]
                                                offset = plane.offset % mmap.PAGESIZE
                                                with mmap.mmap(plane.fd, offset + stream.stride * stream.size.height,
                                                               flags=mmap.MAP_SHARED, prot=mmap.PROT_READ,
                                                               offset=plane.offset - offset) as pixels:
                                                    image = np.ndarray((stream.size.height, stream.stride),
                                                                       dtype=np.uint8, buffer=pixels, offset=offset)
                                                    image = image[:, :stream.size.width].copy()
                                                cv2.imshow("B0267", image)
                                        elif not ready:
                                            streak = 0
                                            previous = None
                                        request.reuse()
                                        if camera.queue_request(request):
                                            raise RuntimeError("Cannot queue camera request")
                                if cv2.waitKey(1) & 0xff in (ord("q"), 27):
                                    return
                                if not ready and not reason and time.monotonic() - started >= ATTEMPT_SECONDS:
                                    reason = "insufficient_frames" if frames else "no_frames"
                        finally:
                            camera.stop()
                    except Exception as error:
                        if not ready:
                            report("error", str(error))
                        raise
                    exhausted = attempt == STARTUP_RETRIES + 1
                    report("exhausted" if exhausted else "retry", reason)
                    if exhausted:
                        raise RuntimeError(f"Camera startup exhausted after {attempt} attempts: {reason}")
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main(cli=True)
