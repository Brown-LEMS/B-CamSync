#!/usr/bin/env python3
"""Open remote board previews in local ffplay windows through an owned SSH tunnel.

Uses only Python's standard library, OpenSSH, and ffplay on the workstation.
The remote checkout must already run examples/opencv_sync.py with its camera SDK.
"""

import argparse
from contextlib import contextmanager, suppress
from html.parser import HTMLParser
from http.client import HTTPConnection
import json
import math
from pathlib import PurePosixPath
import queue
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import quote


class PreviewUnits(HTMLParser):
    def __init__(self):
        super().__init__()
        self.units = []

    def handle_starttag(self, tag, attributes):
        if tag == "img":
            unit = dict(attributes).get("data-unit")
            if unit and unit not in self.units:
                self.units.append(unit)


def fetch(port, path, timeout=1):
    connection = HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def ssh_command(args, local_port):
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ControlPath=none",
               "-o", "ExitOnForwardFailure=yes",
               "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5",
               "-o", "ServerAliveCountMax=2", "-L",
               f"127.0.0.1:{local_port}:127.0.0.1:{args.remote_port}"]
    if args.attach:
        return command + ["-N", "-T", "--", args.host]
    remote = [args.remote_python, "-u", "examples/opencv_sync.py", "--http-preview",
              "--continuous", "--repeat", "1", "--http-port", str(args.remote_port),
              "--preview-fps", str(args.fps), "--", args.config]
    return command + ["-tt", "--", args.host,
                      f"cd {shlex.quote(args.remote_dir)} && exec {shlex.join(remote)}"]


def terminate(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@contextmanager
def tunnel(args):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", args.local_port))
        local_port = reservation.getsockname()[1]
    print(f"Connecting to {args.host}; {'attaching to' if args.attach else 'starting'} remote preview...", flush=True)
    process = subprocess.Popen(ssh_command(args, local_port), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    ready = threading.Event()

    def remote_output():
        for line in iter(process.stdout.readline, b""):
            message = line.decode("utf-8", errors="replace").rstrip()
            print(f"[remote] {message}", file=sys.stderr, flush=True)
            if message == f"Browser preview: http://127.0.0.1:{args.remote_port}":
                ready.set()

    output = threading.Thread(target=remote_output, name="ssh-output")
    output.start()
    try:
        deadline = time.monotonic() + args.startup_timeout
        next_progress = 0
        while True:
            if process.poll() is not None:
                raise RuntimeError(f"SSH or remote application exited ({process.returncode})")
            if args.attach or ready.is_set():
                try:
                    status, page = fetch(local_port, "/")
                except OSError:
                    pass  # The tunnel or remote HTTP listener is still starting.
                else:
                    if status != 200:
                        raise RuntimeError(f"Preview page returned HTTP {status}")
                    parser = PreviewUnits()
                    parser.feed(page.decode("utf-8"))
                    if not parser.units:
                        raise RuntimeError("Remote page contains no board preview images")
                    print(f"Tunnel ready on 127.0.0.1:{local_port}; boards: {', '.join(parser.units)}", flush=True)
                    yield process, local_port, parser.units
                    break
            if time.monotonic() >= deadline:
                raise TimeoutError("Remote preview did not start before its startup deadline")
            if time.monotonic() >= next_progress:
                print("Waiting for remote capture and preview HTTP service...", flush=True)
                next_progress = time.monotonic() + 2
            time.sleep(0.1)
    finally:
        if not args.attach and process.poll() is None:
            print("Stopping the remote preview application started by this tool...", flush=True)
            try:
                # -tt provides a remote foreground terminal: Ctrl+C runs camera cleanup.
                process.stdin.write(b"\x03")
                process.stdin.flush()
                process.wait(timeout=15)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                print("Remote graceful shutdown was not confirmed; closing SSH.", file=sys.stderr)
        terminate(process)
        with suppress(BrokenPipeError):
            process.stdin.close()
        output.join()
        process.stdout.close()


def player_command(executable, host, unit, index, args):
    return [executable, "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer",
            "-flags", "low_delay", "-framedrop", "-an", "-autoexit", "-f", "mjpeg",
            "-framerate", str(args.fps), "-probesize", "32", "-analyzeduration", "0",
            "-window_title", f"{host} / {unit} (Q or Esc to close)",
            "-x", str(args.width), "-left", "40", "-top", str(60 + index * 260),
            "-i", "pipe:0"]


def relay(port, unit, player, done, errors, args):
    started = False
    deadline = time.monotonic() + args.startup_timeout
    try:
        while not done.is_set():
            status, jpeg = fetch(port, f"/preview/{quote(unit, safe='')}.jpg")
            if status == 200:
                player.stdin.write(jpeg)
                player.stdin.flush()
                started = True
            elif status == 204:
                if started:
                    raise RuntimeError(f"{unit}: preview became stale; no fresh frame is available")
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{unit}: no preview frame before the startup deadline")
            else:
                raise RuntimeError(f"{unit}: preview returned HTTP {status}")
            done.wait(1 / args.fps)
    except BrokenPipeError:
        pass  # Closing a player window closes its input; the main thread observes its exit.
    except Exception as error:
        if not done.is_set():
            errors.put(error)


def display(process, port, units, executable, args):
    players, workers = [], []
    done, errors = threading.Event(), queue.Queue()
    try:
        for index, unit in enumerate(units):
            player = subprocess.Popen(player_command(executable, args.host, unit, index, args),
                                      stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
            players.append(player)
            worker = threading.Thread(target=relay, args=(port, unit, player, done, errors, args),
                                      name=f"preview-{unit}")
            workers.append(worker)
            worker.start()
        print("Local preview windows opened. Close a window, press Q/Esc, or use Ctrl+C to exit.", flush=True)
        next_status = 0
        while True:
            if process.poll() is not None:
                raise RuntimeError(f"SSH or remote application exited ({process.returncode})")
            closed = [player.returncode for player in players if player.poll() is not None]
            if closed:
                if any(code != 0 for code in closed):
                    raise RuntimeError(f"ffplay exited with an error: {closed}")
                return
            try:
                error = errors.get_nowait()
            except queue.Empty:
                pass
            else:
                raise error
            if args.attach and time.monotonic() >= next_status:
                status, payload = fetch(port, "/status")
                if status != 200:
                    raise RuntimeError(f"Synchronization status returned HTTP {status}")
                print(json.dumps(json.loads(payload)), flush=True)
                next_status = time.monotonic() + 1
            time.sleep(0.05)
    finally:
        done.set()
        for player in players:
            terminate(player)  # Unblock any worker writing to a paused or closing player.
        for worker in workers:
            worker.join()
        for player in players:
            with suppress(BrokenPipeError):
                player.stdin.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="SSH host alias or user@host; uses your normal SSH configuration")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--remote-dir", help="absolute remote checkout path; starts its opencv_sync.py")
    mode.add_argument("--attach", action="store_true", help="view an already running preview; leave it running on exit")
    parser.add_argument("--config", default="group.json", help="configuration path on the remote device")
    parser.add_argument("--remote-python", default=".venv/bin/python")
    parser.add_argument("--remote-port", type=int, default=8765)
    parser.add_argument("--local-port", type=int, default=0, help="local tunnel port; 0 chooses a free port")
    parser.add_argument("--fps", type=float, default=10, help="preview rate, independent of capture rate")
    parser.add_argument("--width", type=int, default=1280, help="local window width")
    parser.add_argument("--startup-timeout", type=float, default=60)
    args = parser.parse_args()
    if args.host.startswith("-") or any(c in args.host for c in "\r\n\x00"):
        parser.error("invalid SSH destination")
    if args.remote_dir and not PurePosixPath(args.remote_dir).is_absolute():
        parser.error("--remote-dir must be an absolute path on the remote device")
    if not 1 <= args.remote_port <= 65535 or not 0 <= args.local_port <= 65535:
        parser.error("invalid local or remote port")
    if args.width <= 0 or any(not math.isfinite(value) or value <= 0 for value in (args.fps, args.startup_timeout)):
        parser.error("width, fps, and startup timeout must be positive finite values")
    executable = shutil.which("ffplay")
    if not executable or not shutil.which("ssh"):
        parser.error("install OpenSSH and FFmpeg with ffplay on the workstation before running this tool")
    try:
        with tunnel(args) as (process, port, units):
            display(process, port, units, executable, args)
    except KeyboardInterrupt:
        print("Preview closed", flush=True)
    except (OSError, RuntimeError, TimeoutError) as error:
        parser.exit(1, f"remote-preview: {error}\n")


if __name__ == "__main__":
    main()
