# B0267 startup recovery on Raspberry Pi 5

## Startup policy

The local Pi 5 acquisition examples give each board one initial start and up to 20 stream restarts. During startup a cancelled request triggers an immediate restart of that board. A board that does not deliver three consecutive successful frame sequences within its two-second qualification window also restarts. A healthy board continues delivering frames while its peer restarts. Each new attempt keeps the configured camera and its allocated buffers, creates new requests with fresh cookies and discards completions from earlier requests. An acquisition, configuration or request-submission error terminates startup. Once capture has been handed to the application, a running request cancellation is reported as a fault.

In the two-board example, both boards must qualify and their latest sensor timestamps must be within two frame periods of each other before group synchronization starts. The synchronization engine then applies its normal phase-lock criteria. The per-board startup events identify the attempt, outcome, reason, first successful frame time and time to qualification. [Dual-board capture](../examples/opencv_sync_http.py) · [Single-board capture](../examples/single_board_http.py)

The optional managed backend uses the same 20-retry limit for the Pi 5 profile. It also requires three consecutive raw frames with stable exposure and gain, and uses the existing gain warmup when automatic gain calibration is enabled. Its no-valid-frame deadline is at least 1.5 seconds and scales to six frame periods at slow frame rates. It retains the 20-second controls deadline and limits startup recovery to 60 seconds per board. [Managed Arducam backend](../addons/managed/src/arducam.cpp)

## Two-board hardware experiment

The test used `rpi5-1` with two B0267 boards, the stock Pivariety driver, kernel `6.12.109+rpt-rpi-2712`, libcamera package `0.5.2+rpt20250909-1` and the onboard 1 Gbit/s Ethernet link. Board 1 at `i2c@80000` served as the fixed synchronization reference and board 2 was at `i2c@88000`. Each fresh process selected the 5120 × 800 RAW8 sensor mode with a 1280 × 200 preview, 40 fps, 10 ms exposure and gain 1. The runner paused 0.5 seconds between processes. Each round exercised a warm application start on the same powered hardware.

The runner polled the two JPEG endpoints and the application's `/status` every 50 ms. A full pass required JPEGs with valid start and end markers from both boards within 100 seconds of launch, `ready=true` with no synchronization fault within 30 more seconds of observing both JPEGs, and clean shutdown. The per-board camera clocks start at `camera.start()`. The JPEG and synchronization clocks start at process launch and include imports, configuration and publication. The reported synchronization status comes from the timing engine. Optical exposure alignment was outside the measurement. [Runner](../tools/check_camera_startup.py) · [Reproduction command](../tools/README.md#capture-and-recording)

## Results

All 200 processes delivered images from both boards and shut down cleanly. Both boards qualified on their first start in 117 rounds (58.5%). The remaining 83 rounds (41.5%) required at least one stream restart. Synchronization reached tracking in 197 rounds (98.5%). The 400 requested board starts took 535 attempts, including 135 frontend timeouts. Every timeout triggered a retry before a valid application frame arrived. No board exhausted the configured 20 retries.

| Maximum additional starts used by either board | Rounds at this count | Cumulative rounds with both cameras ready |
| ---: | ---: | ---: |
| 0 | 117 | 117/200 (58.5%) |
| 1 | 54 | 171/200 (85.5%) |
| 2 | 18 | 189/200 (94.5%) |
| 3 | 6 | 195/200 (97.5%) |
| 4 | 4 | 199/200 (99.5%) |
| 5 | 1 | 200/200 (100%) |

These counts describe the attempts actually needed in the fixed-cap run. Round 7 needed the sixth start of board 1 and delivered both previews at 8.373 seconds. Board 1 qualified immediately in 142/200 rounds and accounted for 84 retries. Board 2 qualified immediately in 169/200 rounds and accounted for 51 retries. The reference identity, port and start order were fixed together. The median failed attempt took 1.275 seconds on board 1 and 1.367 seconds on board 2.

| Time since launch | Observations | Median | P95 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| Both JPEGs, all starts | 200 | 2.375 s | 5.820 s | 8.373 s |
| Both JPEGs, first-attempt successes | 117 | 2.338 s | 2.397 s | 2.421 s |
| Both JPEGs, at least one restart | 83 | 3.613 s | 6.910 s | 8.373 s |
| Synchronization ready, successful rounds | 197 | 16.542 s | 19.190 s | 28.818 s |

Rounds 41, 110 and 114 had already recovered their camera streams when board 2 reported `startup correction budget exhausted`. Their final window P95 values were 0.288, 0.293 and 0.282 ms, respectively. The phase controller permits five startup corrections, uses a 0.25 ms deadband and requires two qualifying non-overlapping windows. A correction resets its qualifying-window counter. This is a separate limit from the per-camera stream retry count. All 117 first-attempt rounds and 80 of the 83 recovered rounds reached synchronization ready. This single run does not establish why these three rounds reached the correction limit. [Phase controller](../rust/src/phase.rs) · [Default phase policy](../include/bcamsync/phase_policy.hpp)

The formal batch ran from 18:03:22 to 18:59:10 EST on November 25, 2026, within one Pi boot. All 326 periodic samples collected between completed rounds 6 and 199 showed `get_throttled=0x0`, with SoC temperature between 58.4 and 65.0 °C. The filtered kernel log contained 535 warnings about the out-of-range 1536 Mbps receive rate. Its counts by port matched the number of stream starts on each port, including successful starts. The hardware, network link and software versions remained fixed during the batch. Two foreground preflight runs and an interrupted background launch that inherited an ignored SIGINT were retained separately. The final launch restored SIGINT so each test process could release its cameras normally.

The [round table](measurements/pi5-retry-200-rounds.csv) contains one row per fresh process, including board attempts, both preview times, synchronization outcome, final phase statistics and the corresponding log name. The [attempt table](measurements/pi5-retry-200-attempts.csv) contains all 535 board attempts with reasons and timestamps. Per-trial application logs, the original JSONL, environment samples, SHA-256 manifest and analysis script are under the local `results/pi5-retry-200-Fh08PRPO/` directory. All 535 board-attempt records were checked against their per-round application logs. The SHA-256 manifest covers the 200 logs and the original JSONL.

## Full-resolution managed RAW capture

After the formal run, a separate C++ client exercised the managed backend with both full-resolution R16 streams, a 640 × 480 viewfinder per board, fixed 10 ms exposure and gain 1. The revised backend qualified both boards in all eight fresh processes, including one process that needed two restarts. All eight then reported `insufficient frame coverage` during synchronization. In eight runs with the same client and the pre-change backend from `219cd72`, five exited on a cancelled startup request and the other three reached the same coverage fault. Both versions were compiled in Release mode with the same compiler, libcamera and installed B-CamSync core.

Separate public-backend probes read the actual timing histories after capture had started successfully. In the last two-second interval shared by both boards, the revised backend supplied 67–68 samples per board in two probes, and the pre-change backend supplied 67–69 in two probes. The measured rates in these windows were 33.4–34.1 fps. The 40 fps phase policy requires at least 72 samples per board in that window. In the revised probes the median sensor timestamp interval was about 25 ms, with 13 intervals of about 50 ms per board in the final window. These measurements reproduce an existing RAW delivery-rate limitation under this configuration. They do not qualify synchronized 40 fps full-resolution RAW recording. [Coverage check](../rust/src/phase.rs) · [Managed capture configuration](../addons/managed/src/arducam.cpp)

Four independent single-board HTTP runs delivered a JPEG and exited cleanly. The Linux example, GUI simulation and measurement suites passed 49 Python tests in total. Native startup-policy and session-lifecycle tests also passed. The simulated exhaustion tests exercised all 21 permitted starts. Physical validation of the OpenCV window itself was outside the test.
