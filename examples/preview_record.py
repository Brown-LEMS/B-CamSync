"""Remote preview and downstream MJPEG recording on a camera node.

Start capture with POST /start, then independently start/stop recording with
POST /record/start and POST /record/stop. Each board writes a full-resolution
four-view grayscale JPEG stream and matching JSONL frame metadata on the Pi.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

from bcamsync import Config
from bcamsync.management import LIMIT
from bcamsync.node import run_node
from preview import PreviewHandler, PreviewService, encode_packet


class RecordingHandler(PreviewHandler):
    def do_POST(self):
        if self.path in {"/record/start", "/record/stop"}:
            try:
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("recording requests require Content-Length")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= LIMIT:
                    raise ValueError("recording request exceeds limit")
                raw = self.rfile.read(length)
                if len(raw) != length or (json.loads(raw) if raw else {}) != {}:
                    raise ValueError("recording requests require an empty JSON object")
            except (ValueError, UnicodeDecodeError) as error:
                self._reply(400, {"ok": False, "error": str(error)})
                return
            self._dispatch(self.path.removeprefix("/"))
        else:
            super().do_POST()


class RecordingService(PreviewService):
    """One frame consumer per board feeds recording and sampled browser preview."""

    def __init__(self, controller, address, *, output_dir, recording_quality=90, **options):
        if not isinstance(recording_quality, int) or not 1 <= recording_quality <= 95:
            raise ValueError("recording_quality must be in 1..95")
        super().__init__(controller, address, **options)
        self.handler_class = RecordingHandler
        self.output_dir = Path(output_dir).resolve()
        self.recording_quality = recording_quality
        self._record_lock = threading.RLock()
        self._control_lock = threading.RLock()
        self._files = None
        self._streams = {}
        self._recording = {"active": False, "directory": None, "fault": "", "units": {}}
        self._next_report = 0.0

    def recording_status(self):
        with self._record_lock:
            return json.loads(json.dumps(self._recording))

    def request(self, command, operation_id=None):
        if command in {"record/start", "record/stop"}:
            if operation_id is not None:
                raise ValueError("recording commands do not accept operation_id")
            with self._control_lock:
                if command == "record/start":
                    self.start_recording()
                else:
                    self.stop_recording()
                return {"node_id": self.local_node, "recording": self.recording_status()}
        if command in {"start", "stop"}:
            with self._control_lock:
                result = super().request(command, operation_id)
                if command == "stop":
                    self.stop_recording()
        else:
            result = super().request(command, operation_id)
        if command in {"start", "stop", "status"}:
            result["recording"] = self.recording_status()
        return result

    def start_recording(self):
        if not super().request("status")["active"]:
            raise RuntimeError("Start capture before recording")
        with self._record_lock:
            if self._files is not None:
                raise RuntimeError("Recording is already active")
            directory = self.output_dir / str(uuid4())
            directory.mkdir(parents=True)
            with ExitStack() as files:
                streams = {}
                manifest = {"config": json.loads(self.config.to_json()),
                            "node_id": self.local_node, "jpeg_quality": self.recording_quality,
                            "layout": "four views concatenated horizontally, uint8 grayscale",
                            "units": {}}
                for index, unit_id in enumerate(self.units):
                    video, metadata = f"board-{index}.mjpg", f"board-{index}.jsonl"
                    streams[unit_id] = (files.enter_context((directory / video).open("wb")),
                                        files.enter_context((directory / metadata).open("w")))
                    manifest["units"][unit_id] = {"video": video, "metadata": metadata}
                (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
                self._files = files.pop_all()
            self._streams = streams
            self._recording = {"active": True, "directory": str(directory), "fault": "",
                               "units": {unit: {"frames": 0, "bytes": 0, "sequence_gaps": 0,
                                                "last_sequence": None} for unit in self.units}}
            self._next_report = time.monotonic() + 5
            print(f"Recording started: {directory}", flush=True)

    def consume_packet(self, packet):
        # Encode boards concurrently; a stopped run cannot write into a later run.
        with self._record_lock:
            files = self._files
            if files is None:
                return
        try:
            encoded = encode_packet(packet, packet.width, self.recording_quality)
        except Exception as error:
            with self._record_lock:
                if files is self._files:
                    self._recording["fault"] = str(error)
            raise
        with self._record_lock:
            if files is not self._files:
                return
            try:
                video, metadata = self._streams[packet.unit_id]
                offset = video.tell()
                video.write(encoded)
                metadata.write(json.dumps({
                    "sequence": packet.sequence, "sensor_timestamp_ns": packet.sensor_timestamp_ns,
                    "timestamp_ns": packet.timestamp_ns, "clock_valid": packet.clock_valid,
                    "dropped_before": packet.dropped_before,
                    "exposure_us": packet.exposure_us, "analogue_gain": packet.analogue_gain,
                    "gain_locked": packet.gain_locked,
                    "view_shape": list(packet.views[0].shape), "views": len(packet.views),
                    "offset": offset, "length": len(encoded),
                }, allow_nan=False) + "\n")
                stats = self._recording["units"][packet.unit_id]
                if stats["last_sequence"] is not None:
                    stats["sequence_gaps"] += max(0, packet.sequence - stats["last_sequence"] - 1)
                stats["last_sequence"] = packet.sequence
                stats["frames"] += 1
                stats["bytes"] += len(encoded)
                if time.monotonic() >= self._next_report:
                    print("Recording progress: " + json.dumps(self._recording["units"]), flush=True)
                    self._next_report = time.monotonic() + 5
            except Exception as error:
                self._recording["fault"] = str(error)
                raise

    def stop_recording(self):
        with self._record_lock:
            if self._files is None:
                return
            files, self._files = self._files, None
            self._recording["active"] = False
            try:
                files.close()
            except OSError as error:
                self._recording["fault"] = str(error)
                raise
            finally:
                self._streams.clear()
            directory = Path(self._recording["directory"])
            (directory / "summary.json").write_text(json.dumps(self._recording, indent=2) + "\n")
            print("Recording stopped: " + json.dumps(self._recording), flush=True)

    def close(self):
        try:
            super().close()
        finally:
            self.stop_recording()

    def page(self):
        return super().page().replace(
            b'<pre id="status">',
            b'<button onclick="control(\'record/start\')">Start recording</button>'
            b'<button onclick="control(\'record/stop\')">Stop recording</button><pre id="status">')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("node")
    parser.add_argument("--address", required=True, help="existing Pi IPv4 address reachable from the host")
    parser.add_argument("--output-dir", required=True, type=Path, help="Pi-local recording directory")
    parser.add_argument("--http-port", type=int, default=58083)
    parser.add_argument("--discovery-port", type=int, default=58082)
    parser.add_argument("--preview-fps", type=float, default=5)
    parser.add_argument("--recording-quality", type=int, default=90)
    args = parser.parse_args()
    run_node(Config.from_file(args.config), args.node, address=args.address,
             network_startup="explicit", server_class=RecordingService,
             output_dir=args.output_dir, recording_quality=args.recording_quality,
             preview_fps=args.preview_fps, http_port=args.http_port,
             discovery_port=args.discovery_port)


if __name__ == "__main__":
    main()
