# Intermittent B0267 startup on Raspberry Pi 5

On 2026-11-25, the same board on the same port sometimes delivered all 12 requested buffers and sometimes stalled after zero, one or two. The stall reproduced with the B0267 streaming straight into the RP1 CSI-2 capture channel using `v4l2-ctl`. This places a reproducible failure in the acquisition path shared by the board, its kernel driver, the CSI-2 link and RP1 capture.

## Hardware and software

The test machine was `rpi5-1`, a Raspberry Pi 5 Model B Rev 1.1 running Debian Bookworm arm64 and kernel `6.12.109+rpt-rpi-2712`. Both B0267 boards reported firmware `0x10004` and two data lanes. The vendor builds were rpicam-apps `v1.9.0 a819af48889f-dirty`, libcamera `v0.5.2+125-69c6546d-dirty` and libpisp `v1.2.1 981977ff21f3`. The initial rpicam-hello and direct capture measurements used `eth1` for management with no carrier on onboard Ethernet. The historical probe replay used onboard Ethernet. The operator's earlier estimates of approximately 30% and 80% startup success under different interference conditions come from separate experiments.

| rpicam index on this boot | libcamera ID component | Sensor subdevice | RP1 CFE | FE image node |
| --- | --- | --- | --- | --- |
| 0 | `i2c@88000/arducam_pivariety@c` | `arducam-pivariety 10-000c` | `1f00110000.csi` | `/dev/video12` |
| 1 | `i2c@80000/arducam_pivariety@c` | `arducam-pivariety 11-000c` | `1f00128000.csi` | `/dev/video4` |

The initial logs are in the local, Git-ignored directory `results/pi5-startup-cb7Ad4OX/`. The historical probe replay logs are in `results/pi5-b0267-restart-eth0/`. Identify the selected sensor and its connected capture node on the current boot with `rpicam-hello --list-cameras` and `media-ctl -p -d /dev/mediaN`.

## First attempt with rpicam

Each trial started one fresh process and one board, repeating the order 0, 1, 1, 0. Set `camera` to the selected index. The command requested the 5120×800 8-bit sensor mode, 1280×200 viewfinder, 40 fps, 10 ms exposure and gain 1.

```sh
LIBCAMERA_LOG_NO_COLOR=1 LIBCAMERA_LOG_LEVELS='RPI:DEBUG,V4L2:DEBUG' \
  rpicam-hello -n --camera "$camera" --viewfinder-mode 5120:800:8 \
  --viewfinder-width 1280 --viewfinder-height 200 --framerate 40 \
  --shutter 10000 --gain 1 -t 250 --verbose 2
```

Count a first-attempt pass when the application reports a viewfinder frame before the first frontend timeout. rpicam restarts the camera after a timeout. All 16 processes eventually exited with status zero, including the six whose first attempt timed out. The 250 ms application duration and the 1 s frontend dequeue watchdog are separate timers.

| Index | First-attempt passes | Timeouts | CFE image buffers dequeued before the first timeout |
| --- | ---: | ---: | --- |
| 0 | 5/8 | 3 | One in each of rounds 04, 05 and 08 |
| 1 | 5/8 | 3 | Zero in each of rounds 03, 14 and 15 |
| Both | 10/16 | 6 | Three zero-buffer and three one-buffer stalls |

Both boards reached `Camera started!`. In the one-buffer cases the logs show a frame-start event, a CFE image dequeue and PiSP processing of that buffer before the next CFE image dequeue timed out. The missing `arducam-pivariety_mono.json` warning also appeared in successful processes. The small per-port samples describe these trials only. The evidence is in `baseline.log` and `01.app` through `16.app`.

### How many starts each process needed

Counting each `Camera started!` before the first viewfinder frame gives 10/16 processes with a frame on the first start, 13/16 within two starts, 15/16 within three and 16/16 within six. In round 14 the application logged five consecutive frontend timeouts and first displayed a frame about 6.55 seconds after the initial request queue. Each retry stopped the camera and started a fresh set of requests in the same process. The public Arducam libcamera pipeline cancels its pending requests on a frontend timeout. The public Raspberry Pi and Arducam command-line applications handle the cancellation by stopping and restarting capture. The public source describes this mechanism, and the target's logs establish that its vendor build actually used it. [Arducam pipeline timeout handler](https://github.com/ArduCAM/libcamera/blob/e4f106ed38b9da6b3a812666036fa6adb29257c4/src/libcamera/pipeline/rpi/common/pipeline_base.cpp#L1406-L1424) · [Arducam application restart loop](https://github.com/ArduCAM/libcamera-apps/blob/ddacad9f864aed593f8af7e2c6167243b4799925/apps/rpicam_hello.cpp#L19-L41)

## Five-second watchdog control

A second series set `camera_timeout_value_ms` to 5000 in a temporary copy of the installed PiSP pipeline configuration, selected it with `LIBCAMERA_RPI_CONFIG_FILE` and used `-t 800` with a 13 s outer process limit. The same alternating order and sensor settings yielded 4/6 first-attempt passes on index 0 and 6/6 on index 1. Index 0 rounds 01 and 08 each dequeued one CFE image before stalling for the full five seconds. Round 08 repeated that failure after rpicam restarted the camera and reached the outer limit.

During those two first attempts the upper 16 bits of `CSI2_CH_DEBUG(0)`, the receiver's frame counter, stayed at one for the remainder of the watchdog interval. In successful round 04 on that port the count advanced through multiple frames. See `window.log` and `window/`. [CSI-2 driver register interpretation](https://github.com/raspberrypi/linux/blob/7d1826930811232688a50c99c540fbb137aed081/drivers/media/platform/raspberrypi/rp1_cfe/csi2.c)

## Direct CSI-2 capture

For index 0 the test temporarily routed `csi2` source pad 4 directly to `rp1-cfe-csi2_ch0` on `/dev/media0`, disabled the PiSP FE links, and selected `Y16 ` on `/dev/video8`. The sensor source remained 5120×800 `Y8_1X8`. Each fresh process queued four MMAP buffers and requested 12 dequeues with a four-second outer deadline.

```sh
v4l2-ctl -d /dev/video8 --set-fmt-video=width=5120,height=800,pixelformat='Y16 '
timeout -k 1 4 stdbuf -oL -eL v4l2-ctl -d /dev/video8 \
  --stream-mmap=4 --stream-count=12 --verbose
```

All 20 calls to `VIDIOC_STREAMON` succeeded. Thirteen attempts delivered all 12 buffers. Of the seven that reached the four-second deadline, two returned zero buffers (16, 20), two returned one (13, 17), and three returned two (08, 09, 11). The saved media graph and capture node format were byte-identical before the test and after restoration. See `direct.sh`, `direct.log` and `direct/`.

### How much of each buffer was written

A separate 16-start series filled each MMAP buffer with `0xa5a5` words before every queue and checked every active pixel after dequeue. Thirteen starts delivered the requested eight buffers, and three stalled. Ten of the 16 sequence-zero buffers contained just three written rows out of 800. The other 797 retained the sentinel, although the kernel reported `bytesused=8192000` and left `V4L2_BUF_FLAG_ERROR` clear. Round 03 recovered from a three-row first buffer and captured eight buffers. Rounds 07 and 09 returned an 800-row first buffer and then stalled. Both the kernel's byte count and the coverage of the first buffer alone would miss relevant failures. See `check_raw_rows.c`, `rows.log` and `rows/`.

### Control-bus timing

Twelve further direct captures traced the board on I²C adapter 10 at address `0x0c`. Seven captured eight buffers and five stalled. Every attempt issued the same 14 register writes in the same order, excluding the two-byte address selections for reads. These comprised stream-on, six control selector/value pairs and stream-off. Across all 744 adapter transfer results, the reported message count equaled the requested count. An I²C acknowledgement establishes completion on the bus. The transmitter's internal state remained unobserved. See `i2c.log` and `i2c/`. [Pivariety stream-on and control replay](https://github.com/raspberrypi/linux/blob/7d1826930811232688a50c99c540fbb137aed081/drivers/media/i2c/arducam-pivariety.c)

Relative to the stream-on register write, the three-row buffers were timestamped at +234.4 to +234.5 ms. Even the later partial buffers were timestamped by +258.8 ms. The first control selector write came at +270.8 to +278.4 ms. All seven successful trials began a sequence of 800-row buffers at approximately +356 ms and continued at 25 ms intervals. Timing uses the buffers' kernel monotonic timestamps and the I²C monotonic trace clock. The filtered `vb2_v4l2_buf_done` tracepoint produced no events in this series, so the comparison does not measure when userspace received each buffer.

A second 12-start series recorded the generic kernel `vb2_buf_done` event together with I²C. Matching buffer index and monotonic timestamp identified all 61 buffers actually dequeued across seven successful and five stalled starts. Five buffers with only three written rows completed in interrupt context at +234.46 to +234.52 ms, at least 39 ms before the first control selector write. In successful round 01, a later 455-row buffer was timestamped at +258.80 ms but completed at +351.40 ms, after the control writes. In failed round 12 the corresponding buffer acquired a timestamp at +258.83 ms and was returned only during stream-off cleanup. Cleanup events were excluded from delivered-frame counts. See `dma-trace/01.trace` and `dma-trace/12.trace`.

Both trace series captured reads of the board's busy register (`0x0107`). All 14 successful and 10 stalled starts observed it clear. From the stream-on write to the first zero reply, the ranges were 270.77–278.42 ms for successes and 270.82–278.34 ms for stalls. After each of the six control writes, every trial observed one busy reply followed by a zero reply, and the driver's final check also read zero. In the DMA trace series, four of the five stalled runs completed one or two buffers in interrupt context before that first zero and none afterward. The fifth had no interrupt-context completions. The command interface reported readiness in every failed trial while continuous image delivery never began.

## Receiver link-rate warning

The kernel logged a calculated link rate of 1536 Mbps per lane and a D-PHY out-of-range warning in all 20 direct trials. Both sensor subdevices exposed a fixed `pixel_rate` of 384,000,000 pixels/s and no `link_freq` control in the captured inventory. The driver calculation matches 384,000,000 × 8 bits ÷ 2 data lanes = 1,536,000,000 bits/s per lane. The D-PHY table ends at 1500 Mbps and selects its highest frequency band for this value. The warning establishes which setting the receiver used. Determining the actual transmitter line rate requires independent evidence. The same warning accompanied 13 successful starts and seven stalls. [CFE rate calculation](https://github.com/raspberrypi/linux/blob/7d1826930811232688a50c99c540fbb137aed081/drivers/media/platform/raspberrypi/rp1_cfe/cfe.c#L1060-L1105) · [D-PHY frequency selection](https://github.com/raspberrypi/linux/blob/7d1826930811232688a50c99c540fbb137aed081/drivers/media/platform/raspberrypi/rp1_cfe/dphy.c#L101-L130)

In eight index 0 rpicam starts, the first D-PHY status sample showed the lane 1 high-speed start-of-transmission synchronization error bit in all five first-attempt passes and all three stalls. The VC0 packet CRC error bit appeared in five passes and two stalls. These error flags clear on read. Sampling began after camera startup, without a per-trial pre-start clear. The snapshots show that the receiver latched real errors, but cannot date each error to the current startup, yield packet error rates or identify a flag unique to the stalls. See `phy.log`, `phy.sh` and `phy/`. [Raspberry Pi register definitions and read-to-clear semantics](https://github.com/raspberrypi/linux/commit/82852abc86d8ee803fcd20b45a3e6e10ae8c0a3d)

All six enumerated sensor modes were opened individually at 30 fps. The final selected modes in the application logs matched the requests. For each of 2560×400, 5120×720 and 5120×800, RAW8 produced the same 1536 Mbps estimate and RAW10 produced 1920 Mbps. Both select the same highest receive band. The final capture returned the sensor to 5120×800 RAW8 at 40 fps. Arducam has confirmed the absence of a reported link frequency for this family. See `modes.log`. [Arducam and Raspberry Pi maintainer discussion](https://github.com/raspberrypi/linux/issues/7310)

## Software trigger and power controls

In 16 rpicam starts, eight trials toggled `trigger_mode` from 0 to 1 for 350 ms and then back to 0, with eight sham trials interleaved. Six pulse trials were already producing frames. Their frame counters nearly stopped during the pulse and resumed afterward. The other two had already stalled and neither recovered. In the sham group, five kept advancing and three remained stalled. See `pulse.log` and `pulse/`.

In 24 direct captures, the control condition started in free-run and the gated condition set `trigger_mode=1` before launch. Both requested `trigger_mode=0` approximately 350 ms after `VIDIOC_STREAMON` returned. Free-run delivered 32 buffers in 8/12 starts and gating in 11/12. In 11 of the 12 gated trials, two or three buffers had already arrived before release. The gated failure stopped at two. See `gate.sh` and `gate.log`.

In a separate interleaved comparison, every trial began with the sensor runtime-suspended. One group used normal runtime power management and delivered eight buffers in 9/12 starts. The other explicitly resumed the sensor 500 ms before launching the capture and kept it active through capture. It delivered eight buffers in 10/12 starts. Both groups reproduced the stall. The power policy was restored to `auto`. See `prewake.log` and `prewake/`.

## What the earlier Pi 5 probe could observe

The August 2026 `bapd8_acquisition` tests used the same B0267 model and Arducam package series on a Pi 5 Rev 1.1. The probes launched `rpicam-raw` for H.264 input or `rpicam-vid` for MJPEG. Their frame-rate calculation used the interval between the first and last recorded timestamps, excluding any delay before the first output. The console printed frame rate, gaps and output size. The JSON summary recorded whether the camera log contained a timeout, and the script required at least two timestamps. The surviving report mentions frontend timeouts during high-load capture. The two original probe directories were absent from the current Pi user's home, and their per-process logs were unavailable in the local project archives. The time of the first frame in those historical runs remains unknown. [August Pi 5 results](https://github.com/xiaoran007/bapd8_acquisition/blob/1ff7a30c89cb5c666457529e2af3a90bfd287976/rpi/recording_probe.md#L14-L60) · [Probe summary calculation](https://github.com/xiaoran007/bapd8_acquisition/blob/984271b013ec4ce1dbd949232cdabf3d9c1b0bf8/rpi/probe_dual_encode.py#L174-L198)

The later acquisition service validated its Pi 4 profile and started libcamera once, with systemd restart disabled. In B-CamSync the single-board and dual-board Python examples likewise call `camera.start()` once and raise an error when a running request is cancelled. The managed backend starts once and reports a fault on cancellation. The startup test also reads the process log and counts a frontend timeout as a failed trial. [Historical platform check](https://github.com/xiaoran007/bapd8_acquisition/blob/e9523d1f9ea8e0eae720777cea494bc10baa5527/rpi/config.py#L201-L202) · [Historical native start](https://github.com/xiaoran007/bapd8_acquisition/blob/e9523d1f9ea8e0eae720777cea494bc10baa5527/rpi/native/capture_encode.cpp#L442) · [Historical service](https://github.com/xiaoran007/bapd8_acquisition/blob/e9523d1f9ea8e0eae720777cea494bc10baa5527/deployment/rpi/bapd8-rpi.service#L17) · [B-CamSync startup test](../tools/check_camera_startup.py)

### Replaying the historical RAW capture settings

With `eth0` linked at 1 Gbit/s, each process captured one board using the historical RAW settings. The experiment repeated board indices 0, 1, 1, 0, saved presentation timestamps and traced six-byte I²C writes to the two CamArrays at address `0x0c`. Raw output went to `/dev/null` to isolate camera startup from encoding and storage. The capture command below shows the four-second variant. The second series used the historical probe's default ten-second recording duration.

```sh
rpicam-raw --camera "$camera" --timeout 4000 --nopreview --denoise off \
  --mode 5120:800:8:P --framerate 45 --buffer-count 8 \
  --save-pts "$trial.pts" --output /dev/null --verbose 2
```

| Capture duration | Processes | First application frame before any restart | At least two output timestamps | Exit code 0 |
| --- | ---: | ---: | ---: | ---: |
| 4 s | 16 | 12 | 14 | 16 |
| 10 s | 8 | 6 | 8 | 8 |

In the ten-second series, rounds 01 and 05 each restarted once before the first application frame. Both recorded 372 timestamps at 45.020 fps. The six processes that succeeded immediately recorded 425–430 timestamps at the same rate. In the four-second series, round 12 restarted twice and recorded 42 timestamps at 45.020 fps. Rounds 05 and 07 each restarted three times and exited with zero timestamps. The raw application checks its recording deadline after receiving a usable request and before submitting that buffer for output. This allows a process to recover after its recording window expires and exit successfully with an empty timestamp file. The historical Python probe would have rejected these two empty recordings in its summary calculation. [Raspberry Pi RAW event loop](https://github.com/raspberrypi/rpicam-apps/blob/v1.9.0/apps/rpicam_raw.cpp#L37-L65)

All eleven pre-frame restarts across the two series matched a frontend timeout followed by host writes of 0 and then 1 to the selected board's `MODE_SELECT` register (`0x0100`). The intervals from the stream-off write to the next stream-on write were 11.98–13.11 ms. The Pi's boot ID stayed the same, and each restart occurred within the original application process. The I²C trace identifies a host-issued stream cycle. It cannot reveal separate reset activity inside the CamArray. See `four-seconds/12.app`, `four-seconds/12.trace`, `ten-seconds/01.pts` and the other per-trial files under `results/pi5-b0267-restart-eth0/`. These trials changed the capture profile as well as the Ethernet connection compared with the earlier rpicam-hello run, so they do not isolate the Ethernet effect.

### Public reports for this camera family

Arducam support described a cold-boot timeout on the B0266 stereo kit with UC-512 Rev.D1 and firmware `0x10003`, and reported that a Rev.D2 replacement started successfully. Its workaround was a second application invocation. A separate Raspberry Pi kernel report covers the OV9281 quad kit and gaps during capture. Arducam acknowledged that the driver exposes pixel rate without a link-frequency control. Neither report establishes the cause of repeated warm-start stalls with B0267 firmware `0x10004`. [Arducam B0266 support thread](https://forum.arducam.com/t/arducam-pivariety-b0266-camera-times-out-on-first-boot-works-after-restart-on-pi-5/10182) · [Raspberry Pi kernel issue 7310](https://github.com/raspberrypi/linux/issues/7310)

## Remaining causal test

Incomplete startup images are visible in direct DMA buffers, and their timestamps precede the post-start control replay. The underlying cause remains unresolved. Two software comparisons could separate the transmitter control sequence from receiver configuration. First, keep RP1 settings fixed while varying when Pivariety replays its six controls. Then hold the board mode, controls and timing fixed while varying only the RP1 high-speed receive frequency setting. Score each condition by first-attempt stalls and actual row coverage through the onset of regular frames.
