"""Preview one B0267 board on a Pi 5. One board needs no synchronization."""

import argparse
from contextlib import ExitStack
import mmap
import selectors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("camera_id", nargs="?", help="board ID; defaults to the first ID in sorted order")
    parser.add_argument("--list", action="store_true", help="list camera IDs without starting capture")
    args = parser.parse_args()

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
        requests = []
        for buffer in allocator.buffers(stream.stream):
            request = camera.create_request()
            if request.add_buffer(stream.stream, buffer):
                raise RuntimeError("Cannot attach camera buffer")
            requests.append(request)
        if camera.start(controls):
            raise RuntimeError("Cannot start camera")
        stack.callback(camera.stop)
        for request in requests:
            if camera.queue_request(request):
                raise RuntimeError("Cannot queue camera request")

        print("Previewing one board; press q or Esc in the window, or Ctrl+C, to exit.")
        with selectors.DefaultSelector() as selector:
            selector.register(manager.event_fd, selectors.EVENT_READ)
            try:
                while True:
                    if selector.select(0.05):
                        for request in manager.get_ready_requests():
                            if request.status != libcamera.Request.Status.Complete:
                                raise RuntimeError("Camera request was cancelled")
                            buffer = request.buffers[stream.stream]
                            if buffer.metadata.status == libcamera.FrameMetadata.Status.Success:
                                plane = buffer.planes[0]
                                offset = plane.offset % mmap.PAGESIZE
                                with mmap.mmap(plane.fd, offset + stream.stride * stream.size.height,
                                               flags=mmap.MAP_SHARED, prot=mmap.PROT_READ,
                                               offset=plane.offset - offset) as pixels:
                                    image = np.ndarray((stream.size.height, stream.stride),
                                                       dtype=np.uint8, buffer=pixels, offset=offset)
                                    image = image[:, :stream.size.width].copy()
                                cv2.imshow("B0267", image)
                            request.reuse()
                            if camera.queue_request(request):
                                raise RuntimeError("Cannot queue camera request")
                    if cv2.waitKey(1) & 0xff in (ord("q"), 27):
                        break
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
