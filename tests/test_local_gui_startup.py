"""Exercise camera startup and keyboard exit without opening a GUI window."""

from contextlib import ExitStack, redirect_stdout
import io
import json
import sys
from tempfile import TemporaryFile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import numpy as np

import test_examples_startup as startup


class LocalGuiStartupTests(unittest.TestCase):
    def test_timing_only_qualifies_and_observes_both_boards_without_copying_pixels(self):
        example = startup.load_example("opencv_sync")
        with ExitStack() as stack:
            capture, _, _, _, _ = startup.StartupTests().fixture(example, stack)
            capture.start(None)
            self.assertTrue(capture.ready.is_set())
            self.assertIsNone(capture.error)
            self.assertFalse(capture.images)
            for timing in capture.units.values():
                self.assertGreater(timing.observe.call_count, 0)

    def test_dual_restarts_only_the_silent_board(self):
        example = startup.load_example("opencv_sync")
        with ExitStack() as stack:
            capture, cameras, requests, manager, _ = startup.StartupTests().fixture(example, stack)
            def completions():
                return startup.completed(requests if cameras[0].start.call_count > 1 else requests[1:])
            manager.get_ready_requests.side_effect = completions
            with patch.object(example, "ATTEMPT_SECONDS", 0.06), redirect_stdout(io.StringIO()):
                capture.start(np)
            self.assertEqual([camera.start.call_count for camera in cameras], [2, 1])
            self.assertEqual(set(capture.images), {"board-1", "board-2"})

    def run_single(self, cancel):
        example = startup.load_example("single_board")
        camera, sdk, gui = Mock(id="camera-a"), MagicMock(), Mock()
        selector = MagicMock()
        selector.__enter__.return_value = selector
        selector.select.return_value = [object()]
        cameras = sdk.CameraManager.singleton.return_value
        cameras.get.return_value = camera
        camera.acquire.return_value = camera.configure.return_value = camera.start.return_value = 0
        camera.queue_request.return_value = 0
        sdk.PixelFormat = str
        sdk.Size.side_effect = lambda *_: SimpleNamespace(width=4, height=2)
        sdk.FrameMetadata.Status.Success = "success"
        sdk.Request.Status.Complete = "complete"
        config = camera.generate_configuration.return_value
        config.validate.return_value = "valid"
        stream = config.at.return_value
        stream.stride = 4
        requests = []
        def create(cookie):
            request = Mock(cookie=cookie, buffers={stream.stream: buffer}, status="complete")
            request.add_buffer.return_value = 0
            requests.append(request)
            return request
        def deliver():
            current = requests[-1]
            current.status = "cancelled" if cancel(current.cookie) else "complete"
            buffer.metadata.sequence += 1
            return [requests[0], current] if len(requests) > 1 else [current]
        camera.create_request.side_effect = create
        cameras.get_ready_requests.side_effect = deliver
        gui.waitKey.side_effect = lambda _: 27 if gui.imshow.called else -1
        output = io.StringIO()
        with TemporaryFile() as storage:
            storage.write(b"12345678")
            storage.flush()
            buffer = SimpleNamespace(planes=[SimpleNamespace(fd=storage.fileno(), offset=0)],
                                     metadata=SimpleNamespace(status="success", sequence=0))
            allocator = sdk.FrameBufferAllocator.return_value
            allocator.allocate.return_value = 0
            allocator.buffers.return_value = [buffer]
            with (patch.dict(sys.modules, {"libcamera": sdk, "cv2": gui, "bcamsync": None}),
                  patch.object(sys, "argv", ["single_board.py", "-v", "camera-a"]),
                  patch.object(example.selectors, "DefaultSelector", return_value=selector),
                  redirect_stdout(output)):
                try:
                    example.main()
                finally:
                    camera.release.assert_called_once()
                    gui.destroyAllWindows.assert_called_once()
                    self.assertEqual(camera.stop.call_count, camera.start.call_count)
                    self.events = [json.loads(line.partition("B-CamSync startup ")[2])
                                   for line in output.getvalue().splitlines() if "B-CamSync startup " in line]
        return gui

    def test_single_stale_request_after_restart_is_discarded_and_escape_stops_capture(self):
        gui = self.run_single(lambda attempt: attempt == 1)
        self.assertEqual([(e["attempt"], e["outcome"]) for e in self.events], [(1, "retry"), (2, "ready")])
        pixels = gui.imshow.call_args.args[1]
        self.assertEqual(pixels.tobytes(), b"12345678")
        self.assertEqual(self.events[-1]["valid_frames"], 3)

    def test_single_exhausts_after_twenty_restarts(self):
        with self.assertRaisesRegex(RuntimeError, "exhausted after 21 attempts"):
            self.run_single(lambda _: True)
        self.assertEqual([e["attempt"] for e in self.events], list(range(1, 22)))
        self.assertEqual([e["outcome"] for e in self.events], ["retry"] * 20 + ["exhausted"])
