# Integrate B-CamSync into an application

[User guide](../README.md) · [First run on Pi 5](getting-started.md) · [Example catalog](../examples/README.md)

This library adds continuous synchronization to a camera application. Start with `SyncSession`: your existing pipeline supplies sensor timing and applies requested frame-period controls through `SyncUnit` adapters. This library estimates phase, decides bounded corrections, and reports quality. Your application keeps camera configuration, acquisition threads, buffers, frame delivery, processing, and recording. The separately installed C++ managed capture add-on provides `Session` for applications that want camera operations and frame delivery handled for them.

| Responsibility | Recommended: `SyncSession` | Optional add-on: `Session` |
| --- | --- | --- |
| Configure, start, and close cameras | Application | Managed capture add-on |
| Own capture buffers and deliver pixels | Application keeps its existing image path | Managed capture add-on provides local frame packets |
| Supply sensor timing and apply frame-period controls | Application's `SyncUnit` adapters | Managed capture add-on's adapters |
| Estimate phase, decide corrections, report quality | This library | This library |
| Display, encode, and record | Application | Application |

## Attach to an existing pipeline: `SyncSession`

Keep your existing capture pipeline and supply one `SyncUnit` adapter per local synchronization unit. For B0267, each board is one unit with four view positions. Your camera SDK must expose sensor timestamps, observed frame durations, and request controls for bounded frame-period changes. Reading image arrays through OpenCV alone does not supply this timing/control contract. The real-device implementation is `CameraTiming` in [`opencv_sync.py`](../examples/opencv_sync.py); the script's `Capture` class supplies those SDK operations and images separately.

Start with the [Pi 5 walkthrough](getting-started.md) to run the real application. To learn the adapter without cameras, the synchronization-only installation also runs this complete synthetic example:

```sh
.venv/bin/python examples/external_capture.py
```

In your pipeline, collect metadata in the existing capture completion callback, apply the adapter's next frame duration when queueing SDK requests, and pass the adapters to `SyncSession`. Your pixel path stays in the application. The example script's recording code can be replaced without changing this library's synchronization runtime.

The lifecycle below is illustrative: `capture` and `adapters` are objects supplied by your application, as in those executable examples.

```python
from bcamsync import SyncConfig, SyncSession

config = SyncConfig.from_file("group.json")
capture.start()
try:
    with SyncSession(config, "node-a", adapters) as sync:
        if not sync.wait_ready(config.startup_timeout_seconds):
            raise RuntimeError(sync.status().fault or "Synchronization readiness timed out")
        capture.run_application_loop(sync)
    # Capture is still running here, with baseline frame-period controls restored.
finally:
    capture.stop()
```

The adapter map's keys must match the configured local unit IDs. `SyncConfig` describes synchronization membership, reference, frame rate, phase policy, and network/clock settings; it does not initialize cameras or apply image/exposure/gain settings. In [`external_one_node_pi5.json`](../examples/configs/external_one_node_pi5.json), `camera_id` belongs to the example application and is read by `Capture`; `SyncConfig` reads the synchronization fields. Set the same baseline frame rate in your capture system and synchronization configuration.

Keep acquisition and request completion running while `SyncSession.stop()` restores baseline controls and waits for feedback. Close the application capture only after synchronization stops. To restart synchronization on those same cameras, call `stop()` followed by `start()` on the same session, as the OpenCV example does. Adapter state must honor the new control round. After a fault, recovery requires an explicit stop/start.

### Adapter methods

Methods can run on the synchronization thread concurrently with your capture callbacks. Protect shared history and control state, and keep image processing out of these callbacks. Derive from `SyncUnit` and call `super().__init__()` in Python.

| Method | Required behavior |
| --- | --- |
| `bind(session_id, round_id)` | Bind the control namespace for this synchronization run, without starting or stopping cameras. |
| `snapshot()` | Return a consistent `TimingSnapshot`: unit ID, current local boot time, ordered frame history, and correction feedback. |
| `adjust(pulse)` | Accept or reject the bounded frame-period correction and return its current `CorrectionStatus`. Acceptance and submission are separate from observed completion. |
| `cancel()` | Request restoration of the baseline frame period while capture continues; report its observed completion in subsequent snapshots. |
| `fault()` | Return an empty string when healthy, or a capture/control error description. |

### Timing observations

Preserve `FrameTiming.sensor_timestamp_ns` as the original sensor timestamp, never application arrival time. Set `timestamp_ns` to that observation in the node's local boot-time domain; for Linux libcamera this is `CLOCK_BOOTTIME`, so the two values are equal. Other SDK clock domains require an explicit conversion into this local domain and a reported `mapping_span_ns` for its uncertainty. The runtime maps local observations into group time. `SyncSession.clock()` exposes the current mapping for your own metadata; check `valid` before using it, and obtain a current mapping as needed.

Supply monotonic camera sequences and ordered, fresh timing history. Record requested and device-reported frame durations in microseconds, plus the correction ID associated with the submitted request. `TimingSnapshot.snapshot_timestamp_ns` and correction restoration timestamps use local boot-time nanoseconds. Use `boot_time_ns()` for local observations and `monotonic_ns()` for the actuator's submission deadline argument, as the examples do.

### Frame-period corrections

Use `PhaseActuator` to track bounded pulses, expiry, cancellation, and restoration. Bind it in `bind()`, call `adjust()` with the current binding and actual device duration limits, and use `next(baseline_us, monotonic_ns())` when submitting each request. Apply the returned `duration_us` through your SDK and retain the `Submission` alongside that request.

Call `completed()` for a restoration submission only when device completion metadata confirms that the baseline duration has taken effect. Queueing a request or accepting a pulse does not confirm restoration. The OpenCV adapter retains pending restoration feedback across requests until the reported duration returns to baseline; use that real-device example for this distinction. Export the actuator's current `snapshot()` as `TimingSnapshot.correction`.

## Optional managed capture: `Session`

First install the separate C++ add-on using the [optional managed installation](installation.md#optional-managed-capture). The recommended synchronization-only installation cannot enumerate cameras through `enumerate_arducam_cameras()` or run managed `Session` acquisition. Prepare a separate managed configuration:

```sh
cp examples/configs/one_node_two_boards_pi5.json managed-group.json
```

Replace both camera ID placeholders with the IDs from your Python libcamera enumeration. The template's `arducam_b0267_pi5` profiles select Pi 5 acquisition. Start from [`capture.py`](../examples/capture.py), which reads all configured local units and handles group shutdown. This smaller single-node loop shows where to place processing:

```python
from bcamsync_managed import Config, Session

config = Config.from_file("managed-group.json")
local_units = [unit for unit in config.units if unit.node_id == "node-a"]
with Session(config, "node-a") as cameras:
    if not cameras.wait_ready(config.startup_timeout_seconds):
        raise RuntimeError(cameras.status().fault or "Synchronization readiness timed out")
    for _ in range(100):
        status = cameras.status()
        if status.fault or not status.ready:
            raise RuntimeError(status.fault or "Synchronization readiness lost")
        for unit in local_units:
            packet = cameras.read(unit.id, timeout_seconds=1)
            if packet is not None:
                camera0, camera1, camera2, camera3 = packet.views
                print(packet.unit_id, packet.sequence, camera0.shape)
```

Entering the context starts acquisition and synchronization. `wait_ready()` waits for a quality condition; it does not start capture. Exiting stops synchronization and closes cameras. You can also call `start()` and `stop()` explicitly. Stopped sessions can be started again.

`read()` returns a local board packet or `None` on timeout. One B0267 packet contains four readonly NumPy views of the same board frame. The views retain their storage, including after the packet or session is released. Copy a view if you need writable pixels. Retaining many frames consumes the bounded image pool and can cause later delivery drops; timing observation and synchronization continue independently. `sequence` and cumulative `dropped_before` expose delivery discontinuities.

Reads continue while synchronization is starting or faulted. Inspect `status().ready`, `status().fault`, and per-unit phase metrics according to your application's acceptance policy. `packet.clock_valid` describes clock mapping, independently of phase readiness. `packet.sensor_timestamp_ns` preserves source time; `packet.timestamp_ns` is mapped group time and is zero when mapping is unavailable. A ready status is a sampled quality report, not an automatic guarantee about every packet retained by the application. Match independent board packets using timestamps when your downstream processing needs corresponding frames.

## Select configuration and quality policy

For the recommended Pi 5 `SyncSession` first run, use [`external_one_node_pi5.json`](../examples/configs/external_one_node_pi5.json). For a timing-only multi-node configuration, see [`external_two_nodes.json`](../examples/configs/external_two_nodes.json). All nodes in a group must agree on membership, reference unit, frame rate, clock mode, and phase policy. The reference is explicitly configured; node discovery resolves known members rather than electing a topology.

A single-node group uses its local clock without networking. Physical multi-node sessions additionally use discovery/transport and private clock estimation, even for application-owned capture. See [deployment](deployment.md) for address policy and optional persistent services. `SyncSession` changes acquisition ownership; it uses the same multi-node runtime facilities as `Session`.

Default readiness targets are p95 ≤ 0.5 ms and max ≤ 1 ms across two settled windows. Continue observing status during acquisition; applications choose whether to stop recording, mark intervals, or continue when quality changes. The example applications illustrate different policies: the OpenCV run fails on sampled readiness loss, while browser recording writes immediately when requested and retains metadata for downstream selection.

For C++, the equivalent public interfaces are [`session.hpp`](../addons/managed/include/bcamsync/session.hpp), [`sync.hpp`](../include/bcamsync/sync.hpp), [`sync_unit.hpp`](../include/bcamsync/sync_unit.hpp), and [`timing.hpp`](../include/bcamsync/timing.hpp). See the [installation guide](installation.md#c) and [CMake consumer](../examples/cmake_consumer) for linking. Public types, API comments, and executable examples define the implemented contract; [design notes](design.md) explain the internals.
