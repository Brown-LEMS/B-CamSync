"""Two-node examples: detected membership, local capture, and preview lifecycle."""

import builtins
from contextlib import ExitStack, redirect_stdout
from io import BytesIO, StringIO
import json
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import numpy as np
from PIL import Image

from test_examples import load_example
from test_examples_http import encoder, request


window = load_example("two_nodes_sync")
http = load_example("two_nodes_sync_http")
EXAMPLES = (window, http)
NETWORK = ["--node", "node-b", "--address-a", "192.168.0.105", "--address-b", "192.168.0.106"]


class TwoNodeExamplesTests(unittest.TestCase):
    def test_both_scripts_build_the_same_four_board_network_topology(self):
        configs = [example.sync_config("192.168.0.105", "192.168.0.106") for example in EXAMPLES]
        self.assertEqual(configs[0].to_json(), configs[1].to_json())
        config = configs[0]
        self.assertEqual((config.network_mode, config.clock_mode), ("existing", "network"))
        self.assertEqual((config.fps, config.reference_unit), (40, "board-a1"))
        self.assertEqual([(node.id, node.address, node.port) for node in config.nodes],
                         [("node-a", "192.168.0.105", 58080), ("node-b", "192.168.0.106", 58080)])
        self.assertEqual([(unit.id, unit.node_id) for unit in config.units],
                         [("board-a1", "node-a"), ("board-a2", "node-a"),
                          ("board-b1", "node-b"), ("board-b2", "node-b")])
        self.assertEqual(config.clock_port, 58081)

    def test_detected_counts_define_identical_membership_in_both_scripts(self):
        for boards_a, boards_b in ((1, 1), (1, 2), (2, 1), (2, 2)):
            with self.subTest(boards_a=boards_a, boards_b=boards_b):
                configs = [example.sync_config("192.168.0.105", "192.168.0.106", boards_a, boards_b)
                           for example in EXAMPLES]
                self.assertEqual(configs[0].sync_json(), configs[1].sync_json())
                self.assertEqual([unit.id for unit in configs[0].units],
                                 [f"board-a{index}" for index in range(1, boards_a + 1)] +
                                 [f"board-b{index}" for index in range(1, boards_b + 1)])
                self.assertEqual(configs[0].reference_unit, "board-a1")

    def test_startup_inventory_exchanges_and_confirms_asymmetric_counts(self):
        for example in EXAMPLES:
            with self.subTest(example=example.__name__):
                stream = MagicMock()
                stream.__enter__.return_value = stream
                stream.readline.side_effect = [b'{"node":"node-b","boards":1}\n', b'ready\n']
                connection = MagicMock()
                connection.__enter__.return_value = connection
                connection.makefile.return_value = stream
                server = MagicMock()
                server.__enter__.return_value = server
                server.accept.return_value = connection, ("192.168.0.106", 12345)
                with patch.object(example.socket, "socket", return_value=server), redirect_stdout(StringIO()):
                    self.assertEqual(example.discover_boards("node-a", "192.168.0.105", "192.168.0.106", 2), (2, 1))
                server.bind.assert_called_once_with(("192.168.0.105", 58080))
                self.assertEqual(json.loads(stream.write.call_args.args[0]), {"node-a": 2, "node-b": 1})
                stream.readline.side_effect = [b'{"node-a":2,"node-b":1}\n']
                stream.write.reset_mock()
                with patch.object(example.socket, "create_connection", return_value=connection), \
                        redirect_stdout(StringIO()):
                    self.assertEqual(example.discover_boards("node-b", "192.168.0.105", "192.168.0.106", 1), (2, 1))
                self.assertEqual(json.loads(stream.write.call_args_list[0].args[0]), {"node": "node-b", "boards": 1})
                self.assertEqual(stream.write.call_args_list[1].args[0], b"ready\n")

    def test_list_needs_no_network_arguments_image_imports_or_clock_executables(self):
        cameras = [Mock(id="camera-2"), Mock(id="camera-1")]
        sdk = SimpleNamespace(CameraManager=SimpleNamespace(
            singleton=lambda: SimpleNamespace(cameras=cameras)))
        for example in EXAMPLES:
            with self.subTest(example=example.__name__), \
                    patch.dict(sys.modules, {"libcamera": sdk, "cv2": None, "numpy": None}), \
                    patch.object(sys, "argv", [example.__name__, "--list"]), \
                    patch.object(example.shutil, "which", side_effect=AssertionError("Clock lookup")), \
                    patch.object(example, "discover_boards") as inventory, \
                    patch.object(example, "SyncSession") as session, \
                    patch.object(example, "Capture") as capture, redirect_stdout(StringIO()) as output:
                example.main()
                self.assertEqual(output.getvalue().splitlines(), ["camera-1", "camera-2"])
                capture.assert_not_called()
                session.assert_not_called()
                inventory.assert_not_called()
        for camera in cameras:
            camera.acquire.assert_not_called()

    def test_invalid_arguments_and_missing_chrony_fail_before_opening_cameras(self):
        invalid = [[], NETWORK + ["one", "two", "three"], NETWORK + ["same", "same"],
                   ["--node", "node-a", "--address-a", "not-an-ip", "--address-b", "192.168.0.106"],
                   ["--node", "node-a", "--address-a", "192.168.0.105", "--address-b", "192.168.0.105"]]
        for example in EXAMPLES:
            for arguments in invalid:
                with self.subTest(example=example.__name__, arguments=arguments), \
                        patch.object(sys, "argv", [example.__name__, *arguments]), \
                        patch.dict(sys.modules, {"libcamera": None, "cv2": None, "numpy": None}), \
                        patch.object(example, "Capture") as capture, \
                        patch("sys.stderr", new_callable=StringIO), self.assertRaises(SystemExit) as error:
                    example.main()
                self.assertEqual(error.exception.code, 2)
                capture.assert_not_called()
            with patch.object(sys, "argv", [example.__name__, *NETWORK, "camera-1", "camera-2"]), \
                    patch.object(example.shutil, "which", return_value=None), \
                    patch.object(example, "Capture") as capture, \
                    patch("sys.stderr", new_callable=StringIO) as output, self.assertRaises(SystemExit):
                example.main()
            self.assertIn("install chrony", output.getvalue())
            capture.assert_not_called()

    def test_capture_maps_reusable_local_camera_ids_to_only_the_selected_nodes_units(self):
        for example in EXAMPLES:
            for node, prefix in (("node-a", "a"), ("node-b", "b")):
                with self.subTest(example=example.__name__, node=node):
                    cameras = [Mock(id="local-path-1"), Mock(id="local-path-2")]
                    for camera in cameras:
                        for operation in (camera.acquire, camera.configure, camera.start, camera.queue_request):
                            operation.return_value = 0
                        camera.controls = {"limits": SimpleNamespace(min=10000, max=100000)}
                        camera.create_request.side_effect = lambda cookie: Mock(cookie=cookie, add_buffer=Mock(return_value=0))
                        configuration = camera.generate_configuration.return_value
                        configuration.validate.return_value = "valid"
                        configuration.at.return_value = SimpleNamespace(stream=object())
                    manager = Mock()
                    manager.get.side_effect = dict(zip([camera.id for camera in cameras], cameras)).get
                    allocator = Mock()
                    allocator.allocate.return_value = 0
                    allocator.buffers.return_value = [object()]
                    sdk = SimpleNamespace(
                        CameraManager=SimpleNamespace(singleton=lambda: manager),
                        StreamRole=SimpleNamespace(Viewfinder="preview"),
                        Size=lambda *_: object(), PixelFormat=str, SensorConfiguration=SimpleNamespace,
                        CameraConfiguration=SimpleNamespace(Status=SimpleNamespace(Invalid="invalid")),
                        FrameBufferAllocator=lambda _: allocator,
                        controls=SimpleNamespace(FrameDurationLimits="limits", **{name: name for name in (
                            "AeEnable", "ExposureTimeMode", "AnalogueGainMode", "ExposureTime", "AnalogueGain")}))
                    with patch.dict(sys.modules, {"libcamera": sdk}), ExitStack() as stack:
                        config = example.sync_config("192.168.0.105", "192.168.0.106")
                        capture = example.Capture([camera.id for camera in cameras], config, node, stack)
                        self.assertEqual(list(capture.units), [f"board-{prefix}1", f"board-{prefix}2"])
                        self.assertEqual([entry[2].unit_id for entry in capture.requests.values()],
                                         [f"board-{prefix}1", f"board-{prefix}2"])
                        self.assertEqual(manager.get.call_count, 2)
                    for camera in cameras:
                        camera.stop.assert_called_once()
                        camera.release.assert_called_once()

    def test_main_selects_local_ids_loads_images_first_and_preserves_local_order(self):
        original_import = builtins.__import__
        manager = SimpleNamespace(cameras=[])
        sdk = SimpleNamespace(CameraManager=SimpleNamespace(singleton=lambda: manager))
        for example in EXAMPLES:
            for available, ids, selected in ((["camera-3", "camera-2", "camera-1"], [], ["camera-1", "camera-2"]),
                                             (["camera-3", "camera-1"], ["camera-3", "camera-1"], ["camera-3", "camera-1"]),
                                             (["camera-3"], [], ["camera-3"]),
                                             (["camera-3", "camera-1"], ["camera-3"], ["camera-3"])):
                with self.subTest(example=example.__name__, ids=ids):
                    manager.cameras = [Mock(id=camera_id) for camera_id in available]
                    imported = set()

                    def load(name, *args, **kwargs):
                        if name in ("cv2", "numpy"):
                            imported.add(name)
                        return original_import(name, *args, **kwargs)

                    def capture(camera_ids, config, local_node, stack):
                        self.assertEqual(imported, {"cv2", "numpy"})
                        self.assertEqual(camera_ids, selected)
                        self.assertEqual(local_node, "node-b")
                        self.assertEqual(config.reference_unit, "board-a1")
                        self.assertEqual([unit.id for unit in config.units if unit.node_id == "node-b"],
                                         [f"board-b{index}" for index in range(1, len(selected) + 1)])
                        raise RuntimeError("capture reached")

                    with ExitStack() as stack:
                        stack.enter_context(patch.dict(sys.modules, {"libcamera": sdk, "cv2": Mock()}))
                        stack.enter_context(patch.object(sys, "argv", [example.__name__, *NETWORK, *ids]))
                        stack.enter_context(patch.object(example.shutil, "which", return_value="/usr/sbin/chronyd"))
                        inventory = stack.enter_context(patch.object(example, "discover_boards", return_value=(2, len(selected))))
                        stack.enter_context(patch("builtins.__import__", side_effect=load))
                        stack.enter_context(patch.object(example, "Capture", side_effect=capture))
                        if example is http:
                            stack.enter_context(patch.object(example, "WebPreview"))
                        stack.enter_context(redirect_stdout(StringIO()))
                        with self.assertRaisesRegex(RuntimeError, "capture reached"):
                            example.main()
                        inventory.assert_called_once_with("node-b", "192.168.0.105", "192.168.0.106", len(selected))

    def test_http_serves_two_local_images_with_global_status_and_collapsed_details(self):
        preview = http.WebPreview("127.0.0.1", 0, "node-b", ("board-b1", "board-b2"))
        try:
            with redirect_stdout(StringIO()):
                preview.start()
            code, _, page = request(preview, "/")
            self.assertEqual(code, 200)
            self.assertIn(b'data-src="/board-b2.jpg"', page)
            self.assertNotIn(b'data-src="/board-a1.jpg"', page)
            self.assertLess(page.index(b'data-src="/board-b2.jpg"'), page.index(b"<details>"))
            self.assertNotIn(b"<details open", page)
            self.assertEqual(request(preview, "/board-b1.jpg")[0], 204)
            self.assertEqual(request(preview, "/board-a1.jpg")[0], 404)
            status = {"local_node": "node-b", "state": "waiting", "ready": False,
                      "waiting_nodes": ["node-a"], "units": [{"id": "board-a1"}, {"id": "board-b1"}]}
            images = {board: np.full((8, 32), 80 + 40 * index, dtype=np.uint8)
                      for index, board in enumerate(("board-b1", "board-b2"))}
            with patch.dict(sys.modules, {"cv2": encoder()}):
                preview.publish(images, status)
            self.assertEqual(json.loads(request(preview, "/status")[2]), status)
            for index, board in enumerate(images):
                code, headers, data = request(preview, f"/{board}.jpg")
                self.assertEqual((code, headers["Content-Type"]), (200, "image/jpeg"))
                with Image.open(BytesIO(data)) as image:
                    self.assertEqual(image.size, (32, 8))
                    self.assertEqual(image.getpixel((0, 0)), 80 + 40 * index)
        finally:
            preview.close()
        self.assertFalse(preview.thread.is_alive())

    def test_http_shows_only_one_board_when_only_one_was_detected(self):
        preview = http.WebPreview("127.0.0.1", 0, "node-b", ("board-b1",))
        try:
            with redirect_stdout(StringIO()):
                preview.start()
            page = request(preview, "/")[2]
            self.assertIn(b'data-src="/board-b1.jpg"', page)
            self.assertNotIn(b'data-src="/board-b2.jpg"', page)
            self.assertEqual(request(preview, "/board-b2.jpg")[0], 404)
            with patch.dict(sys.modules, {"cv2": encoder()}):
                preview.publish({"board-b1": np.full((8, 32), 80, dtype=np.uint8)},
                                {"state": "calibrating", "ready": False})
            self.assertEqual(request(preview, "/board-b1.jpg")[0], 200)
        finally:
            preview.close()

    def test_group_stop_keeps_local_preview_running_and_restores_before_capture_closes(self):
        for example in EXAMPLES:
            for state, fault in (("stopped", ""), ("fault", "peer disconnected")):
                with self.subTest(example=example.__name__, state=state):
                    events = []
                    capture = Mock(units={"board-b1": Mock(), "board-b2": Mock()},
                                   images={"board-b1": object(), "board-b2": object()},
                                   lock=threading.Lock(), error=None)
                    capture.start.side_effect = lambda: events.append("capture-start")
                    capture.close.side_effect = lambda: events.append("capture-close")
                    session = Mock()
                    session.status.return_value = SimpleNamespace(
                        state=state, ready=False, fault=fault, units=[], waiting_nodes=[],
                        stop_node="node-a", stop_reason="fault" if fault else "requested")
                    session.clock.return_value = SimpleNamespace(valid=False, uncertainty_ns=0)
                    session.stop.side_effect = lambda: events.append("sync-stop")
                    cv2 = Mock()
                    cv2.waitKey.side_effect = [-1, -1, ord("q")]
                    preview = Mock()
                    with ExitStack() as stack:
                        stack.enter_context(patch.dict(sys.modules, {"cv2": cv2}))
                        stack.enter_context(patch.object(sys, "argv", [example.__name__, *NETWORK, "cam-1", "cam-2"]))
                        stack.enter_context(patch.object(example.shutil, "which", return_value="/usr/sbin/chronyd"))
                        stack.enter_context(patch.object(example, "discover_boards", return_value=(2, 2)))
                        stack.enter_context(patch.object(example, "Capture", return_value=capture))
                        create_session = stack.enter_context(patch.object(example, "SyncSession", return_value=session))
                        if example is http:
                            create_preview = stack.enter_context(patch.object(example, "WebPreview", return_value=preview))
                            stack.enter_context(patch.object(example.time, "sleep", side_effect=[None, KeyboardInterrupt]))
                        stack.enter_context(redirect_stdout(StringIO()))
                        example.main()
                    self.assertEqual(create_session.call_args.args[1:], ("node-b", capture.units))
                    self.assertEqual(events, ["capture-start", "sync-stop", "capture-close"])
                    if example is http:
                        self.assertEqual(create_preview.call_args.args[2:], ("node-b", ("board-b1", "board-b2")))
                        self.assertEqual(preview.publish.call_count, 2)
                        self.assertEqual(preview.publish.call_args.args[1]["stop_node"], "node-a")
                        preview.close.assert_called_once()
                    else:
                        self.assertEqual(cv2.imshow.call_count, 6)


if __name__ == "__main__":
    unittest.main()
