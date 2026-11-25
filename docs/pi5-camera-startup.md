# Intermittent B0267 startup on Raspberry Pi 5

On 2026-11-25, the same board on the same port sometimes delivered all 12 requested buffers and sometimes stalled after zero, one or two. The stall reproduced with the B0267 streaming straight into the RP1 CSI-2 capture channel using `v4l2-ctl`. This places a reproducible failure in the acquisition path shared by the board, its kernel driver, the CSI-2 link and RP1 capture.

## Hardware and software

The test machine was `rpi5-1`, a Raspberry Pi 5 Model B Rev 1.1 running Debian Bookworm arm64 and kernel `6.12.109+rpt-rpi-2712`. Both B0267 boards reported firmware `0x10004` and two data lanes. The vendor builds were rpicam-apps `v1.9.0 a819af48889f-dirty`, libcamera `v0.5.2+125-69c6546d-dirty` and libpisp `v1.2.1 981977ff21f3`. The network configuration stayed fixed during these trials. Onboard Ethernet `eth0` had no carrier, and the host used `eth1` for the management connection. The operator's earlier estimates of approximately 30% and 80% startup success under different interference conditions come from separate experiments.

| rpicam index on this boot | libcamera ID component | Sensor subdevice | RP1 CFE | FE image node |
| --- | --- | --- | --- | --- |
| 0 | `i2c@88000/arducam_pivariety@c` | `arducam-pivariety 10-000c` | `1f00110000.csi` | `/dev/video12` |
| 1 | `i2c@80000/arducam_pivariety@c` | `arducam-pivariety 11-000c` | `1f00128000.csi` | `/dev/video4` |

All experiment logs cited below are in the local, Git-ignored directory `results/pi5-startup-cb7Ad4OX/`. Identify the selected sensor and its connected capture node on the current boot with `rpicam-hello --list-cameras` and `media-ctl -p -d /dev/mediaN`.

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

## Receiver link-rate warning

The kernel logged a calculated link rate of 1536 Mbps per lane and a D-PHY out-of-range warning in all 20 direct trials. Both sensor subdevices exposed a fixed `pixel_rate` of 384,000,000 pixels/s and no `link_freq` control in the captured inventory. The driver calculation matches 384,000,000 × 8 bits ÷ 2 data lanes = 1,536,000,000 bits/s per lane. The D-PHY table ends at 1500 Mbps and selects its highest frequency band for this value. The warning establishes which setting the receiver used. Determining the actual transmitter line rate requires independent evidence. The same warning accompanied 13 successful starts and seven stalls. [CFE rate calculation](https://github.com/raspberrypi/linux/blob/7d1826930811232688a50c99c540fbb137aed081/drivers/media/platform/raspberrypi/rp1_cfe/cfe.c#L1060-L1105) · [D-PHY frequency selection](https://github.com/raspberrypi/linux/blob/7d1826930811232688a50c99c540fbb137aed081/drivers/media/platform/raspberrypi/rp1_cfe/dphy.c#L101-L130)

In eight index 0 rpicam starts, the first D-PHY status sample showed the lane 1 high-speed start-of-transmission synchronization error bit in all five first-attempt passes and all three stalls. The VC0 packet CRC error bit appeared in five passes and two stalls. These error flags clear on read. Sampling began after camera startup, without a per-trial pre-start clear. The snapshots show that the receiver latched real errors, but cannot date each error to the current startup, yield packet error rates or identify a flag unique to the stalls. See `phy.log`, `phy.sh` and `phy/`. [Raspberry Pi register definitions and read-to-clear semantics](https://github.com/raspberrypi/linux/commit/82852abc86d8ee803fcd20b45a3e6e10ae8c0a3d)

All six enumerated sensor modes were opened individually at 30 fps. The final selected modes in the application logs matched the requests. For each of 2560×400, 5120×720 and 5120×800, RAW8 produced the same 1536 Mbps estimate and RAW10 produced 1920 Mbps. Both select the same highest receive band. The final capture returned the sensor to 5120×800 RAW8 at 40 fps. Arducam has confirmed the absence of a reported link frequency for this family. See `modes.log`. [Arducam and Raspberry Pi maintainer discussion](https://github.com/raspberrypi/linux/issues/7310)

## Software trigger and power controls

In 16 rpicam starts, eight trials toggled `trigger_mode` from 0 to 1 for 350 ms and then back to 0, with eight sham trials interleaved. Six pulse trials were already producing frames. Their frame counters nearly stopped during the pulse and resumed afterward. The other two had already stalled and neither recovered. In the sham group, five kept advancing and three remained stalled. See `pulse.log` and `pulse/`.

In 24 direct captures, the control condition started in free-run and the gated condition set `trigger_mode=1` before launch. Both requested `trigger_mode=0` approximately 350 ms after `VIDIOC_STREAMON` returned. Free-run delivered 32 buffers in 8/12 starts and gating in 11/12. In 11 of the 12 gated trials, two or three buffers had already arrived before release. The gated failure stopped at two. See `gate.sh` and `gate.log`.

In a separate interleaved comparison, every trial began with the sensor runtime-suspended. One group used normal runtime power management and delivered eight buffers in 9/12 starts. The other explicitly resumed the sensor 500 ms before launching the capture and kept it active through capture. It delivered eight buffers in 10/12 starts. Both groups reproduced the stall. The power policy was restored to `auto`. See `prewake.log` and `prewake/`.

## Remaining causal test

Incomplete startup images are visible in direct DMA buffers, and their timestamps precede the post-start control replay. The underlying cause remains unresolved. Two software comparisons could separate the transmitter control sequence from receiver configuration. First, keep RP1 settings fixed while varying when Pivariety replays its six controls. Then hold the board mode, controls and timing fixed while varying only the RP1 high-speed receive frequency setting. Score each condition by first-attempt stalls and actual row coverage through the onset of regular frames.
