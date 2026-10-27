# From a blank Raspberry Pi to a B0267 demo

This guide takes you from writing a fresh Raspberry Pi OS card to viewing live camera images on your computer. It assumes no previous Raspberry Pi, SSH, or Python experience. The main route uses one Raspberry Pi 5, one or two Arducam B0267 boards, and a browser on your computer; the Pi does not need a monitor or keyboard.

Follow the numbered sections in order. With one B0267 board, you can check all four camera views. With two boards, you can also run B-CamSync's synchronization demo.

## 1. Gather the equipment

- A Raspberry Pi 5 with a suitable power supply and cooling.
- A microSD card (32 GB or larger is a practical choice) and a card reader for your computer. Writing the OS erases the selected card, so copy off anything you need first.
- One Arducam B0267 board for initial capture, or two for the synchronization demo, with their camera modules and compatible ribbon cables.
- A Windows, macOS, or Linux computer with a terminal and a web browser.
- A network that both your computer and Pi can join, plus internet access on the Pi for software downloads. Ethernet to the same router is also suitable.

The installer supports Raspberry Pi 4 and Pi 5 on **Raspberry Pi OS Bookworm, 64-bit**. This walkthrough and the two-board examples target Pi 5. A standard Pi 4 has one camera connector and cannot follow the two-board Pi 5 setup directly.

## 2. Write the OS with Raspberry Pi Imager

**On your computer:** install and open [Raspberry Pi Imager](https://www.raspberrypi.com/software/). Insert the microSD card into your computer's card reader.

In Imager:

1. Select **Raspberry Pi 5** as the device.
2. Select **Raspberry Pi OS (Legacy, 64-bit)** based on **Debian 12 / Bookworm**. Choose its **Lite** edition for the SSH/browser route here, or its desktop edition if you also want local preview windows later. Check the description: the installer requires Bookworm and arm64. The default OS download is currently Trixie; Bookworm is listed under Legacy on the [official OS download page](https://www.raspberrypi.com/software/operating-systems/).
3. Select your microSD card as the storage destination. Double-check its name and capacity before writing.
4. Continue to OS customisation and enter the settings below. Labels vary between Imager versions.

| Setting | What to enter |
| --- | --- |
| Hostname | `bcam-pi` (example) |
| Username | `camera` (example) |
| Password | A password you choose and keep for SSH login |
| Wi-Fi | Your network name (SSID) and password, if using Wi-Fi |
| Localisation | Your country, time zone, and keyboard layout |
| Remote Access / SSH | Enable SSH and select **password authentication** |

`camera` is the Pi login account you are creating, and `bcam-pi` is the Pi's network name. You may choose different names; replace them in every command below. The SSH password is the account password from Imager, which can differ from your Wi-Fi password. This configuration is supported by Imager's [OS customisation workflow](https://www.raspberrypi.com/documentation/computers/getting-started.html#install-using-imager).

Apply the customisation settings, start writing, and wait for writing and verification to finish. Eject the card using your computer's eject function, then remove it from the reader.

**Checkpoint:** you have a Bookworm 64-bit card with networking, a known username/password, and SSH password login already enabled.

## 3. Connect the cameras and boot the Pi

Keep the Pi disconnected from power while connecting ribbon cables. Insert the microSD card. Connect each B0267 assembly using the connector orientation and power connections shown in the [Arducam CamArray hardware guide](https://docs.arducam.com/Raspberry-Pi-Camera/Multi-Camera-CamArray/quick-start/#hardware-connection). Seat each ribbon straight and close its latch; use the instructions for your board revision to determine which way its contacts face.

For a single board, use the Pi 5 connector labelled **CAM/DISP1**. For two boards, connect one board to **CAM/DISP1** and the other to **CAM/DISP0**. Pi 5 uses smaller 22-pin camera connectors; select cables compatible with both ends, following the [Raspberry Pi connector documentation](https://www.raspberrypi.com/documentation/accessories/camera.html#camera-connector-pinout-22-pin).

If using Ethernet, connect the Pi to your router. Connect power and allow a few minutes for the first boot. Your computer must be able to reach the Pi on the network; a guest or institutional Wi-Fi network may block connections between devices. Use a network that permits them.

## 4. Connect over SSH

SSH opens a command-line session on the Pi from your computer. Open **Terminal** on macOS/Linux, or **PowerShell / Windows Terminal** on Windows.

**On your computer**, run:

```sh
ssh camera@bcam-pi.local
```

Here, `camera` is the username from Imager and `bcam-pi.local` is the hostname followed by `.local`. On the first connection, SSH asks whether to trust the host key. Confirm that you are connecting to your Pi, type `yes`, and press Enter. Enter the password you set in Imager. Password entry displays no characters, dots, or asterisks; type it and press Enter.

After login, a prompt similar to `camera@bcam-pi:~ $` means subsequent commands run **on the Pi**, even though the terminal window is on your computer. For more about addresses and SSH connections, see the [Raspberry Pi remote access guide](https://www.raspberrypi.com/documentation/computers/remote-access.html).

If the hostname does not resolve, find the Pi's IP address in your router's connected-device list and connect using that address, for example:

```sh
ssh camera@192.168.1.42
```

`192.168.1.42` is an example; replace it with your Pi's actual address. If you have a screen and keyboard attached to the Pi, `hostname -I` also prints its addresses.

*Optional*: **On the Pi**, check the model, OS, and architecture:

```sh
cat /proc/device-tree/model
cat /etc/os-release
dpkg --print-architecture
uname -m
```

Expect a Raspberry Pi 5 model, `VERSION_CODENAME=bookworm`, `arm64`, and `aarch64`. If the OS or architecture differs, return to Imager and write the correct image before continuing.

From this point onward, all terminal commands run **inside the SSH session on the Pi**, unless explicitly labelled otherwise. Copy commands without the terminal prompt. Run each block in order and check its result before continuing. `sudo` gives a command administrator privileges; if it requests a password, use your Pi account password.

## 5. Download B-CamSync and prepare the installer

Install the tools required by the camera installer, along with the packages used later for the demos:

```sh
sudo apt update
sudo apt install -y git curl wget unzip dpkg-dev python3-venv python3-opencv libsystemd0
```

`apt` installs system packages. This step may take several minutes; let it finish and return to the prompt. Then download the project:

```sh
cd ~
git clone https://github.com/Brown-LEMS/B-CamSync.git
cd ~/B-CamSync
ls tools/install_b0267_camera_stack.sh
```

`git clone` creates a local copy of the project in `~/B-CamSync`; `~` means your Pi account's home directory, and `cd` changes directory. The final command should print the installer path. The public repository needs no GitHub account or token.

## 6. Install the B0267 camera stack

The camera stack supplies the camera software that B-CamSync's examples use. The helper, [`tools/install_b0267_camera_stack.sh`](../tools/install_b0267_camera_stack.sh), downloads Arducam's installer, verifies it against a fixed SHA-256 checksum, runs its `libcamera_dev` and `libcamera_apps` installations, and configures camera overlays in `/boot/firmware/config.txt`. An overlay tells the OS which camera hardware to load. The helper also sets `camera_auto_detect=0`.

**Choose one command from this table. Run only the row matching your wiring.**

| Connected hardware | Command, from `~/B-CamSync` |
| --- | --- |
| Pi 5, one or two boards | `sudo bash tools/install_b0267_camera_stack.sh` |
| Pi 4, one board on its camera connector | `sudo bash tools/install_b0267_camera_stack.sh cam1` |

On Pi 5, the command above enables both ports, so you can start with one board and add the second later. The helper also accepts `cam1` or `cam0` to enable only that port when explicitly requested; this walkthrough uses the two-port default. On Pi 4, omitting the argument enables the single camera port, and `cam0` is rejected.

Keep power and networking connected while the installer prints download and package-installation progress. It records its work under `/var/log/bapd8/rpi-b0267-camera-stack/<RUN_ID>/`; this path is intentional, and each run has its own timestamped directory. Records include installation logs, package lists, and the boot configuration before and after the changes.

**When installation succeeds, the helper prints `Arducam B0267 camera stack installation completed.` and reboots the Pi automatically.** The SSH session will close. Wait for the Pi to boot again, then run this **on your computer**:

```sh
ssh camera@bcam-pi.local
```

Enter your Pi account password again. If installation ends with an error instead of the completion message, follow the relevant troubleshooting entry below before continuing; a closed SSH connection alone does not establish success.

## 7. Check that the camera stack sees the boards

**On the Pi**, after reconnecting:

```sh
cd ~/B-CamSync
rpicam-still --version
rpicam-hello --list-cameras
```

Expect one camera entry per connected B0267 board: **one board produces one camera ID containing four views**, rather than four separate IDs. With two boards, expect two camera entries. Check that the listed modes include the 5120×800 mode used by these examples.

If no camera appears, stop here and check the selected port, installation logs, and ribbon connections. Before reseating a cable, run `sudo poweroff`, wait for shutdown, and disconnect the power supply. Reconnect power after the wiring is complete.

**Checkpoint:** `rpicam` starts and lists the expected boards. This checks the vendor stack's enumeration; the demo below checks actual image capture.

## 8. Create the Python environment and check camera bindings

A Python virtual environment (venv) holds the project's Python packages. Here it must also see the OS-provided libcamera, NumPy, and OpenCV modules. Create it with the Pi's Bookworm system Python, using `--system-site-packages`:

```sh
cd ~/B-CamSync
python3 -m venv --system-site-packages .venv
.venv/bin/python --version
.venv/bin/python -c 'import cv2, numpy, libcamera; print("Camera and image imports OK"); print(libcamera.SensorConfiguration, libcamera.controls.ExposureTimeMode, libcamera.controls.AnalogueGainMode)'
```

Bookworm's system interpreter is Python 3.11. The final command must print `Camera and image imports OK` and all three camera API objects without an exception. These APIs are required by the demos; camera enumeration by itself does not check them. The vendor runtime and Python libcamera bindings must be compatible.

Use `.venv/bin/python` for all remaining Python commands. You do not need to activate the environment, install Rust, or compile C++. Keep the system image packages for this route; installing a separate NumPy 2 into the venv can conflict with Bookworm's OpenCV binary.

## 9. Install the B-CamSync package

From `~/B-CamSync`, run:

```sh
.venv/bin/python tools/install_ci_wheel.py
```

The tool downloads the core `bcamsync` wheel matching your Python version from the public `rolling` GitHub Release, verifies its size and SHA-256 digest, and installs it in `.venv`. A wheel is a prebuilt Python package. The tool prints the Release URL, its `Commit: ...`, download progress, and `Installed core imports OK`. Save the printed commit with your experiment notes.

The wheel contains the synchronization library. Camera drivers, libcamera bindings, and OpenCV come from the earlier steps. The examples here do not need the optional `bcamsync-managed` add-on, NetworkManager configuration, or chrony setup.

The rolling wheel can be older than the repository you cloned because maintainers publish it separately. Use the examples from the wheel's printed commit. In the following commands, replace `YOUR_WHEEL_COMMIT` with the full hash from the installer's `Commit:` line, without the `Commit:` label:

```sh
git fetch origin
git switch --detach YOUR_WHEEL_COMMIT
git rev-parse HEAD
```

The final output should match the printed wheel commit. A detached checkout simply keeps the source at that specific revision; it does not remove your `.venv`. The camera installation is already applied to the OS. If that revision does not contain the demos described here, ask the project maintainer to publish a wheel for the current examples before continuing.

Check the complete application environment:

```sh
.venv/bin/python -c 'from bcamsync import SyncConfig, SyncSession; from bcamsync.adapters.libcamera import CameraTiming; import cv2, numpy, libcamera; print("B-CamSync and camera pipeline imports OK")'
.venv/bin/python examples/single_board_http.py --list
```

Expect the success message followed by the camera IDs, one per board. `--list` lists devices without starting capture. If other camera models are connected, identify which IDs belong to the B0267 boards before choosing them for a demo.

If the installer reports that a public Release or compatible wheel is unavailable, installation has not completed. Ask the maintainer to publish the required ARM64 wheel; the tool does not automatically build from source. The [installation reference](installation.md#install-a-prebuilt-core-wheel-recommended) also explains how to select a fixed Release for repeatable deployments.

## 10. View one board in your browser

**On the Pi**, run:

```sh
cd ~/B-CamSync
hostname -I
.venv/bin/python examples/single_board_http.py
```

Note the Pi's network IP address from `hostname -I`. The demo prints its selected camera ID and `HTTP preview listening on 0.0.0.0:8000`. It keeps running in the terminal; you should not expect the shell prompt to return yet.

**On your computer**, open your browser and enter `http://PI_IP:8000/`, replacing `PI_IP` with the Pi's address. For example, if the Pi's address is `192.168.1.42`, open `http://192.168.1.42:8000/`. Enter the address in the browser address bar, not in the SSH terminal. `0.0.0.0` is the server's listening address; use the Pi's actual address in your browser.

Expect one grayscale strip with four views. Move an object in front of each camera to check that all four views update. The example configures a 5120×800, 8-bit sensor mode, 40 fps capture, 10 ms exposure, gain 1.0, and a 1280×200 preview. Browser snapshots update at approximately 10 fps, independently of the capture setting.

To select a specific board, stop the demo with **Ctrl+C in the Pi's SSH terminal**, then run the following after replacing `CAMERA_ID` with an ID from `--list`. Keep the quotes around the ID:

```sh
.venv/bin/python examples/single_board_http.py 'CAMERA_ID'
```

Closing the browser tab does not stop capture. Always use Ctrl+C in the Pi terminal before starting another camera program. The HTTP server listens on all Pi interfaces by default and has no login; use it on your lab's trusted network.

**Checkpoint with one board:** four live views establish that the camera capture and browser preview path works. This single-board demo does not import `bcamsync`; the package import check in section 9 verifies installation. To exercise synchronization, continue with two boards.

If you are adding a second board now, stop capture, run `sudo poweroff`, wait for shutdown, disconnect power, and connect the second board to the unused port. Reconnect power, log in over SSH, and repeat section 7 and the `--list` command from section 9. The Pi 5 command used in section 6 already enabled both ports, so this route needs no second camera-stack installation.

## 11. Synchronize two boards

Connect two B0267 boards to the two Pi 5 ports and enable both ports as described above. Both boards must appear in `--list`. Stop the single-board demo with Ctrl+C before starting this one.

**On the Pi**, from `~/B-CamSync`:

```sh
.venv/bin/python examples/opencv_sync_http.py --list
.venv/bin/python examples/opencv_sync_http.py
```

Open the same `http://PI_IP:8000/` page on your computer. Expect two strips, each containing four views, plus synchronization status. The script sorts camera IDs and uses the first two; it prints `board-1 (reference)` and `board-2` at startup. To set the reference explicitly, replace both placeholders with two different IDs from `--list`:

```sh
.venv/bin/python examples/opencv_sync_http.py 'REFERENCE_CAMERA_ID' 'SECOND_CAMERA_ID'
```

The terminal prints `sync=...`, `ready=...`, and `fault=...` once per second, followed by each board's phase, p95, and maximum error in milliseconds. Wait for **`ready=True` in the terminal** (`"ready": true` in the browser). Early zero metrics or visible pictures alone do not establish synchronization. p95 is the error at the 95th percentile of the measurement window; max is its largest error.

Default readiness targets are p95 ≤ 0.5 ms and max ≤ 1 ms over two settled windows. These are configured acceptance thresholds based on sensor timestamps, rather than measured results promised for your setup. Optical alignment and sustained recording throughput require separate experiments. If `ready` remains false or a fault appears, retain the reported state and fault for diagnosis. Preview can continue after synchronization faults; those frames must be treated as unsynchronized.

Use Ctrl+C in the Pi terminal to stop synchronization and capture. Wait for the shell prompt before starting another run. These demos preview images; they do not record video.

## 12. Stop, reconnect, or use a Pi display

For later runs, reconnect **from your computer** with `ssh camera@bcam-pi.local`, enter your Pi password, then run **on the Pi**:

```sh
cd ~/B-CamSync
.venv/bin/python examples/single_board_http.py
```

Use `examples/opencv_sync_http.py` instead for two-board synchronization. You do not need to reinstall the camera stack or recreate `.venv` for each session. The demos do not start automatically at boot.

If you installed the desktop edition and have a monitor attached to the Pi, open a terminal **in the Pi's desktop session** and choose one of these commands:

```sh
cd ~/B-CamSync
.venv/bin/python examples/single_board.py
# Or, with two boards:
.venv/bin/python examples/opencv_sync.py
```

These versions open OpenCV windows on the Pi. They require a graphical session; a plain SSH connection does not provide one. Press q or Esc in a preview window, or Ctrl+C in the terminal, to stop.

To finish using the Pi, stop the demo first. Run `exit` to close only your SSH session. To turn off the Pi, run `sudo poweroff`, wait for shutdown, and then disconnect power.

## Troubleshooting by stage

| Symptom | What to check or do |
| --- | --- |
| `ssh` is not recognised on your Windows computer | Enable/install the Windows OpenSSH Client optional feature, reopen the terminal, and retry. |
| `Could not resolve hostname` | Check the hostname from Imager or connect to the Pi's actual IP address. |
| SSH times out or says `No route to host` | Check power, first-boot progress, the Imager Wi-Fi settings, the Pi's address, and whether the network allows devices to communicate. |
| SSH says `Connection refused` | Check that SSH was enabled in Imager and that the address belongs to the Pi. |
| SSH says `Permission denied` | Use the username and account password created in Imager; there is no assumed `pi` account in this guide. |
| SSH warns that the host key changed after you rewrote the card | After confirming that the address belongs to your newly imaged Pi, run `ssh-keygen -R bcam-pi.local` on your computer, or use the IP address if that is how you connected. Reconnect and accept the new key. |
| Installer requires Bookworm or arm64 | Re-image with Bookworm 64-bit in Imager and reapply username, password, networking, and SSH settings. |
| Installer reports a checksum mismatch | Stop and retain the error. The downloaded vendor installer differs from the pinned checksum; ask the maintainer to review it before proceeding. |
| Installer rejects the boot configuration | It expects exactly one `camera_auto_detect=` line, an `[all]` section, and no Pivariety overlay conflicting with your port selection. Inspect the error and configuration before editing; the helper does not resolve conflicts automatically. |
| Vendor package installation fails | Read the matching install log in the timestamped directory below; keep the OS version, kernel version, and package error when reporting the failure. |
| No cameras, or fewer cameras than expected | Confirm the enabled ports, completion and reboot, and ribbon seating with power disconnected. A board is one camera ID. |
| `ModuleNotFoundError: libcamera`, or a missing camera API | Confirm you are using `.venv/bin/python` and that `.venv` was created with `--system-site-packages`. Install the Python bindings compatible with the vendor camera stack if absent or too old; installing `bcamsync` cannot supply them. |
| `ModuleNotFoundError: cv2` or a NumPy/OpenCV ABI error | Check `python3-opencv` is installed and the venv sees system packages. Use Bookworm's image packages and remove any separately installed incompatible NumPy from the venv. |
| `ModuleNotFoundError: bcamsync` | Repeat section 9 with `.venv/bin/python` and check that the wheel installer completed successfully. |
| Camera cannot be acquired | Stop other camera applications, including demos in other SSH sessions, before running one demo. |
| The browser cannot open the preview | Keep the demo running, use `http://` and the Pi's actual address with port `8000`, and check network reachability. In a second SSH session, `curl http://127.0.0.1:8000/status` checks the server on the Pi itself. |
| `Address already in use` | Stop the previous preview, or run the HTTP demo with `--port 8001` and open the corresponding browser address. |
| OpenCV complains about a display or Qt | Use the HTTP example over SSH, or run the window example from a graphical terminal on the Pi. |

To find a camera installation record and inspect its files **on the Pi**:

```sh
ls -lt /var/log/bapd8/rpi-b0267-camera-stack/
```

Replace `RUN_ID` below with the timestamp directory for your installation:

```sh
ls /var/log/bapd8/rpi-b0267-camera-stack/RUN_ID/
cat /var/log/bapd8/rpi-b0267-camera-stack/RUN_ID/libcamera-dev-install.log
cat /var/log/bapd8/rpi-b0267-camera-stack/RUN_ID/rpicam-apps-install.log
```

For package options, see [Installation](installation.md). For camera selection and synchronization interpretation, see [First run on Pi 5](getting-started.md) and [Examples](../examples/README.md). Recording and workstation preview tools are described in [Developer tools](../tools/README.md).
