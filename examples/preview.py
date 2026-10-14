"""Independent browser preview application using local B-CamSync frames.

Install the optional preview dependency (Pillow), run this application on each
camera node, and open its printed HTTP address. Closing a browser does not stop
acquisition; use the node controls or the group's host management client.
"""

from __future__ import annotations

import argparse
from html import escape
from io import BytesIO
import math
from pathlib import Path
import sys
import threading
import time
from urllib.parse import unquote, urlsplit

import numpy as np
from PIL import Image

from bcamsync import Config
from bcamsync.http_management import HttpNodeService, ManagementHandler
from bcamsync.node import run_node


def encode_packet(packet, max_width: int, quality: int) -> bytes:
    """Render the four local views as one grayscale board strip."""
    pixels = np.concatenate(packet.views, axis=1)
    image = Image.fromarray(pixels)
    if image.width > max_width:
        height = max(1, round(image.height * max_width / image.width))
        image = image.resize((max_width, height), Image.Resampling.BILINEAR)
    output = BytesIO()
    image.save(output, format="JPEG", quality=quality)
    return output.getvalue()


class PreviewHandler(ManagementHandler):
    def _content(self, code: int, payload: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # Browser lifetime does not own acquisition or the producers.
            pass

    def do_GET(self):
        path = urlsplit(self.path).path
        service = self.server.service
        if path == "/":
            self._content(200, service.page(), "text/html; charset=utf-8")
        elif path.startswith("/preview/") and path.endswith(".jpg"):
            unit_id = unquote(path[len("/preview/"):-len(".jpg")])
            if unit_id not in service.units:
                self._content(404, b"Unknown local board", "text/plain")
                return
            image = service.latest_image(unit_id)
            self._content(200 if image else 204, image or b"", "image/jpeg")
        else:
            super().do_GET()


class PreviewService(HttpNodeService):
    """Example application: the sole local frame consumer plus latest JPEG snapshots."""

    def __init__(self, controller, address, *, preview_fps=5.0, max_width=1280,
                 jpeg_quality=80, **service_options):
        if not math.isfinite(preview_fps) or preview_fps <= 0:
            raise ValueError("preview_fps must be finite and positive")
        if not isinstance(max_width, int) or max_width < 1 or not 1 <= jpeg_quality <= 95:
            raise ValueError("max_width must be positive and JPEG quality must be in 1..95")
        super().__init__(controller, address, handler_class=PreviewHandler, **service_options)
        self.units = {unit.id: unit for unit in self.config.units if unit.node_id == self.local_node}
        self.preview_fps = preview_fps
        self.max_width = max_width
        self.jpeg_quality = jpeg_quality
        self._preview_lock = threading.RLock()
        self._readers_changed = threading.Condition(self._preview_lock)
        self._transition_lock = threading.Lock()
        self._readers = set()
        self._generation = 0
        self._publishing = False
        self._images = {}
        self._preview_done = threading.Event()
        self._producers = []
        self._producer_errors = []

    def _invalidate(self):
        with self._preview_lock:
            self._generation += 1
            self._publishing = False
            self._images.clear()
            return self._generation

    def request(self, command, operation_id=None):
        if command not in {"start", "stop"}:
            return super().request(command, operation_id)
        with self._transition_lock:
            status = super().request("status")
            if ((command == "start" and status["active"]) or
                    (command == "stop" and operation_id is not None and status["active"]
                     and status["operation_id"] != operation_id)):
                return super().request(command, operation_id)
            generation = self._invalidate()
            result = super().request(command, operation_id)
            if command == "start":
                with self._preview_lock:
                    if generation == self._generation:
                        self._publishing = result["active"]
            else:
                # Finish in-flight reads before restart reopens the backend queues.
                with self._readers_changed:
                    while self._readers:
                        self._readers_changed.wait()
            return result

    def request_shutdown(self):
        self._preview_done.set()
        self._invalidate()
        super().request_shutdown()

    def close(self):
        self.request_shutdown()
        super().close()
        self._join_producers()

    def _join_producers(self):
        for thread in self._producers:
            if thread is not threading.current_thread() and thread.ident is not None:
                thread.join()

    def serve(self):
        if self._producers:
            raise RuntimeError("Preview application has already been served")
        try:
            for unit_id in self.units:
                thread = threading.Thread(target=self._produce, args=(unit_id,), name=f"preview-{unit_id}")
                self._producers.append(thread)
                thread.start()
            super().serve()
        finally:
            self.request_shutdown()
            self._join_producers()
        if self._producer_errors:
            raise RuntimeError("Preview producer failed: " + "; ".join(self._producer_errors))

    def _produce(self, unit_id):
        next_encode = 0.0
        try:
            while not self._preview_done.is_set():
                status = self.request("status")
                with self._preview_lock:
                    publishing = self._publishing
                if not publishing or not status["active"]:
                    with self._preview_lock:
                        self._images.pop(unit_id, None)
                    self._preview_done.wait(0.1)
                    continue
                due = time.monotonic() >= next_encode
                generation, packet = self._read_packet(unit_id, due)
                if packet is None or not due:
                    continue
                encoded = encode_packet(packet, self.max_width, self.jpeg_quality)
                next_encode = time.monotonic() + 1 / self.preview_fps
                status = self.request("status")
                with self._preview_lock:
                    if (self._publishing and not self._preview_done.is_set() and generation == self._generation
                            and status["active"]):
                        self._images[unit_id] = (time.monotonic(), encoded)
        except Exception as error:
            if not self._preview_done.is_set():
                message = f"{unit_id}: {error}"
                self._producer_errors.append(message)
                print(f"Preview error: {message}", file=sys.stderr, flush=True)
                self.request_shutdown()

    def _read_packet(self, unit_id, drain_latest):
        with self._readers_changed:
            generation = self._generation
            if not self._publishing or self._preview_done.is_set():
                return generation, None
            self._readers.add(unit_id)
        session = None
        try:
            session = self.session
            if session is None:
                return generation, None
            packet = session.read(unit_id, timeout_seconds=0.1)
            if packet is not None and drain_latest:
                for _ in range(self.units[unit_id].queue_capacity):
                    newest = session.read(unit_id, timeout_seconds=0)
                    if newest is None:
                        break
                    packet = newest
            return generation, packet
        except RuntimeError:
            with self._preview_lock:
                if generation != self._generation or self._preview_done.is_set():
                    return generation, None
            raise
        finally:
            session = None
            with self._readers_changed:
                self._readers.discard(unit_id)
                self._readers_changed.notify_all()

    def latest_image(self, unit_id) -> bytes | None:
        with self._preview_lock:
            latest = self._images.get(unit_id)
            if latest is None or time.monotonic() - latest[0] > 1:
                return None
            return latest[1]

    def page(self) -> bytes:
        boards = "".join(f'<section><h2>{escape(unit_id)}</h2><img data-unit="{escape(unit_id)}" hidden></section>'
                         for unit_id in self.units)
        return ("""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>B-CamSync preview</title><style>body{font:16px system-ui;margin:2rem;background:#fafafa}img{max-width:100%;display:block}img[hidden]{display:none}button{padding:.6rem 1rem;margin-right:.5rem}pre{white-space:pre-wrap}</style></head>
<body><h1>Node """ + escape(self.local_node) + """ preview</h1><p>Closing this page leaves acquisition running. Use Stop node to finish.</p>
<button onclick="control('start')">Start node</button><button onclick="control('stop')">Stop node</button><pre id="status">Loading status…</pre>""" + boards + """
<script>
async function control(command){const r=await fetch('/'+command,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});document.querySelector('#status').textContent=JSON.stringify(await r.json(),null,2)}
async function status(){try{const r=await fetch('/status',{cache:'no-store'});document.querySelector('#status').textContent=JSON.stringify(await r.json(),null,2)}catch{document.querySelector('#status').textContent='Node unavailable'}finally{setTimeout(status,1000)}}
function clearImage(img){clearTimeout(img.expires);img.hidden=true;img.removeAttribute('src');if(img.dataset.url){URL.revokeObjectURL(img.dataset.url);delete img.dataset.url}}
async function image(img){try{const r=await fetch('/preview/'+encodeURIComponent(img.dataset.unit)+'.jpg',{cache:'no-store'});if(r.status===200){const url=URL.createObjectURL(await r.blob());clearImage(img);img.dataset.url=url;img.src=url;img.hidden=false;img.expires=setTimeout(()=>clearImage(img),1000)}else{clearImage(img)}}catch{clearImage(img)}finally{setTimeout(()=>image(img),200)}}
status();document.querySelectorAll('img').forEach(image);
</script></body></html>""").encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("node")
    parser.add_argument("--http-port", type=int, default=58083)
    parser.add_argument("--discovery-port", type=int, default=58082)
    parser.add_argument("--address", help="existing local IPv4 address")
    parser.add_argument("--preview-fps", type=float, default=5)
    args = parser.parse_args()
    run_node(Config.from_file(args.config), args.node, address=args.address,
             network_startup="automatic",
             http_port=args.http_port, discovery_port=args.discovery_port,
             server_class=PreviewService, preview_fps=args.preview_fps)


if __name__ == "__main__":
    main()
