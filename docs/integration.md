# Integrate B-CamSync into an application

[User guide](../README.md) · [First run on Pi 5](getting-started.md) · [Example catalog](../examples/README.md)

B-CamSync supplies ongoing frame-phase control, clock mapping, and synchronization status. Choose one of its two supported entry points based on who owns acquisition. Both use the same synchronization runtime; image processing, encoding, recording, and experiment workflow belong to the application.

| Responsibility | Managed: `Session` | Existing pipeline: `SyncSession` |
| --- | --- | --- |
| Configure, start, and close cameras | Library | Application |
| Own capture buffers and deliver pixels | Library provides local frame packets | Application keeps its existing image path |
| Supply sensor timing and apply frame-period controls | Managed backend | Application's `SyncUnit` adapters |
| Estimate phase, decide corrections, report quality | Library | Library |
| Display, encode, and record | Application | Application |

## Let the library operate cameras: `Session`

Use a `Config` containing the topology, camera IDs, managed profiles, and capture settings. Start from [`capture.py`](../examples/capture.py), which reads all configured local units and handles group shutdown. This smaller single-node loop shows where to place processing:

```python
from bcamsync import Config, Session

config = Config.from_file("group.json")
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

## Attach to an existing pipeline: `SyncSession`

Keep your own camera configuration, acquisition thread, buffers, pixel delivery, and recording. Supply one `SyncUnit` adapter per local synchronization unit. For B0267, each board is one unit; it is not one adapter per camera view. The real-device implementation is `CameraTiming` in [`opencv_sync.py`](../examples/opencv_sync.py); [`external_capture.py`](../examples/external_capture.py) is a runnable synthetic implementation with no camera dependency.

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

The adapter map's keys must match the configured local unit IDs. `SyncConfig` describes synchronization membership, reference, frame rate, phase policy, and network/clock settings; it does not initialize cameras or apply image/exposure/gain settings. The Pi 5 configuration can be read as either `Config` or `SyncConfig`, but only the managed path consumes its acquisition settings.

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

## Select configuration and quality policy

For the Pi 5 first run, use [`one_node_two_boards_pi5.json`](../examples/configs/one_node_two_boards_pi5.json). For a timing-only multi-node configuration, see [`external_two_nodes.json`](../examples/configs/external_two_nodes.json). All nodes in a group must agree on membership, reference unit, frame rate, clock mode, and phase policy. The reference is explicitly configured; node discovery resolves known members rather than electing a topology.

A single-node group uses its local clock without networking. Physical multi-node sessions additionally use discovery/transport and private clock estimation, even for application-owned capture. See [deployment](deployment.md) for address policy and optional persistent services. `SyncSession` changes acquisition ownership; it uses the same multi-node runtime facilities as `Session`.

Default readiness targets are p95 ≤ 0.5 ms and max ≤ 1 ms across two settled windows. Continue observing status during acquisition; applications choose whether to stop recording, mark intervals, or continue when quality changes. The example applications illustrate different policies: the OpenCV run fails on sampled readiness loss, while browser recording writes immediately when requested and retains metadata for downstream selection.

For C++, the equivalent public interfaces are [`session.hpp`](../include/bcamsync/session.hpp), [`sync.hpp`](../include/bcamsync/sync.hpp), [`sync_unit.hpp`](../include/bcamsync/sync_unit.hpp), and [`timing.hpp`](../include/bcamsync/timing.hpp). See the [installation guide](installation.md#c) and [CMake consumer](../examples/cmake_consumer) for linking. Public types, API comments, and executable examples define the implemented contract; [design notes](design.md) explain the internals.
