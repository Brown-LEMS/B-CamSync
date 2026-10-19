# Developer tools

## Remote desktop preview

Run `remote_preview.py` on your development workstation. It uses Python 3.11 or later, OpenSSH, and FFmpeg's `ffplay`; it imports no B-CamSync package, camera SDK, OpenCV, or NumPy. The remote Pi must already have this repository, its working camera environment, and a completed camera configuration. SSH must work noninteractively using your normal host configuration, keys, and known-host entry.

Start the existing external-capture application remotely and open one local window per board:

```sh
.venv/bin/python tools/remote_preview.py pi5 \
  --remote-dir /home/bapd8/B-CamSync --config group.json
```

Replace `pi5` with your SSH alias or `user@host`, and use the absolute checkout path on the Pi. `--config` is a remote path, relative to that checkout by default. `--remote-python` defaults to the checkout's `.venv/bin/python`. The tool runs `examples/opencv_sync.py --http-preview --continuous --repeat 1`, forwards the Pi's loopback HTTP port through SSH, and feeds JPEG images to local ffplay windows. Each board image contains its four camera views. It starts no recording and installs or copies no remote files.

To view a preview application that is already running:

```sh
.venv/bin/python tools/remote_preview.py pi5 --attach --remote-port 8765
```

Close any player window, press Q/Esc in a player, or press Ctrl+C in the terminal to close the tool. Launch mode sends Ctrl+C to the remote application it started so the application can restore synchronization controls and release cameras. Attach mode only closes the local players and tunnel; the existing remote application keeps running. An interrupted SSH connection can prevent confirmation of remote cleanup; the tool reports that explicitly.

The local forwarding port is selected automatically and binds only to `127.0.0.1`. `--local-port` selects a fixed port, and `--remote-port` defaults to 8765. Preview defaults to 10 fps independently of acquisition; use `--fps 5` to reduce preview traffic and `--width 960` to resize local windows. Launch mode also sets the remote preview encoder rate; attach mode only changes the local polling rate. Remote progress and synchronization status are printed in the terminal. The tool waits up to `--startup-timeout 60` seconds for startup; transport failures and stale previews end the tool rather than displaying an indefinitely frozen image.

Run its standalone checks with the project environment:

```sh
.venv/bin/python -m unittest discover -s tools -p test_remote_preview.py -v
```

These checks use a local HTTP server and substitute SSH/player executables to exercise process ownership and data flow without opening cameras or windows.
