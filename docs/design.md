# B-CamSync Design

## Library role

B-CamSync (B = Brown) is an independent camera library above camera drivers, intended for Brown lab projects and external researchers. Its primary usage flow is camera initialization followed by frame acquisition through the library. In ordinary use, the application accesses cameras through B-CamSync, which encapsulates the underlying driver and synchronization implementation.

This boundary gives applications one place to manage synchronized camera access. Camera lifecycle, timestamp handling, and synchronization can be coordinated internally, while applications consume frames and synchronization quality information.

## Responsibilities

B-CamSync owns camera initialization, camera access, frame acquisition, timestamp handling, synchronization control, and quality reporting. Its interfaces should follow this usage flow and expose the information applications need to use the resulting frames.

Applications own frame processing, recording policy, privacy controls, retention, segmentation, and data transfer. BAPD8 maintains those responsibilities in its own repository. B-CamSync's design is independent of BAPD8's application scenario.

## Core and integration layer

The synchronization core and application integration layer are separate. The core consumes timing observations and actuation feedback, maintains phase estimates and synchronization state, and produces correction decisions and quality information. It operates independently of pixel delivery, camera SDK calls, recording, and application workflow.

Two application integration modes are intended:

- Managed acquisition: B-CamSync initializes and operates cameras, runs synchronization continuously, and delivers frames to the application. This is the first implementation scope.
- External acquisition: an existing acquisition system supplies timing observations, executes synchronization controls, and reports their completion while retaining its own pixel path and camera lifecycle. This mode shares the synchronization core and will be implemented later.

The managed integration layer owns the acquisition session lifecycle and coordinates backend acquisition, timing observations, correction execution, and frame delivery. Waiting for or processing frames in application code does not drive synchronization progress. In external acquisition mode, the integration layer coordinates the equivalent observations and controls supplied by the application's acquisition system.

Application integration mode and hardware backend are separate dimensions. Integration determines how the application participates in camera acquisition; a backend determines how concrete devices and platform facilities implement acquisition and synchronization controls. Both modes use the same timing and control semantics.

## First implementation: local frame consumption

Applications consume frames locally on the node hosting their cameras. Across nodes, B-CamSync exchanges synchronization observations, control requests, completion feedback, and status. Remote image delivery is outside the first implementation scope.

Each node operates local acquisition, timing observation, and correction execution for its units. One coordinator per synchronization group performs phase estimation and correction decisions centrally, using local and remote observations. Nodes hosting followers execute the coordinator's commands and return completion feedback. Public frame access presents the local camera views, while group status describes the relevant synchronization state.

The primary frame-delivery unit is one board frame packet containing its four camera views, board identity, frame sequence, and timing metadata. The views refer to the same captured board frame and retain its storage. Automatic assembly of corresponding frames from multiple local boards is outside the first frame-access contract.

Frame consumption follows the pull model of [OpenCV VideoCapture](https://docs.opencv.org/4.x/d8/dfe/classcv_1_1VideoCapture.html): applications call a read operation to obtain a frame packet, with acquisition and synchronization continuing independently. B-CamSync uses a bounded FIFO that evicts the oldest undelivered packet. A reusable image pool separates SDK buffer ownership from application retention; if all storage is retained, image delivery is skipped while timing observation and control continue. Capture sequences and cumulative drop counts expose discontinuities. This choice bounds capture-side memory and keeps application processing out of synchronization scheduling.

## Synchronization model

The model separates four concepts:

- A synchronization unit is a device or device group whose frame phase is observed and controlled together. For the initial Arducam backend, one synchronization board is one unit.
- A camera view is an individual image source exposed to the application. Each current Arducam board provides four views, with synchronization controlled at board level and frame access presented at view level.
- A node hosts one or more synchronization units and provides their local clock domain.
- A synchronization group identifies the units whose frame phases are aligned, within one node or across several nodes.

Each group has one reference (master) unit and the remaining units are followers (slaves). Any enumerated unit may be selected as the reference when establishing the group. Selection is independent of unit order or physical placement; all followers align to the selected reference. Reference and follower roles belong to units, independently of node roles: a node can host both a reference board and a follower board.

Synchronization computation is centralized in one group coordinator, which maintains separate measurement and correction state for each follower against the same reference. Each node supplies local timing and executes local controls; a complete phase controller is not independently run on every node. In the initial deployment model, the node hosting the selected reference unit also hosts the coordinator and serves as the network clock reference. These remain distinct responsibilities even when colocated.

| Configuration | Units | Clock arrangement | Phase arrangement |
| --- | --- | --- | --- |
| Eight cameras, two nodes, one board per node | Two boards | Map timestamps to a common clock | One reference and one follower |
| Eight cameras, one node, two boards | Two boards | Shared local clock domain | One reference and one follower |
| Sixteen cameras, two nodes, two boards per node | Four boards | Map timestamps to a common clock | One reference and three followers |

Clock mapping and frame-phase alignment are separate layers. Units on the same node use a shared local time domain and still require ongoing frame-phase observation and correction. Units on different nodes additionally require timestamp mapping into a common clock domain. For the current libcamera path, sensor timestamps use the node's `CLOCK_BOOTTIME` domain.

Closed-loop frame-phase control measures phase differences and drift, applies bounded frame-period adjustments, and restores the baseline period after each correction. Local and remote units share the same estimation and control model; access to their observations and controls is local or networked according to placement.

Synchronization runs continuously after startup calibration, independently of application frame consumption, processing, recording, and persistence. The library reports synchronization state and quality; applications decide how those reports affect their own workflows.

The goal is sub-millisecond synchronization with explicit quality reporting. Synchronization observations and control state should be available alongside frame acquisition so applications can interpret the timing of their data.

## Node clock and network management

For physical multi-node sessions, the node runtime owns a transient synchronization connection and a private clock estimator. The default `network.mode: "auto"` activates a NetworkManager IPv4 link-local profile through D-Bus on the configured interface. The profile is volatile and bound to the runtime's D-Bus connection; it supplies no gateway or DNS. Interface-scoped announcements discover the configured node IDs and verify the shared synchronization contract. Node identities remain stable while resolved addresses belong to the current session.

`network.mode: "existing"` accepts configured numeric IPv4 addresses and retains existing network configuration. Both modes use the same synchronization transport and independent clock estimator. A single-node group uses the sensor's local `CLOCK_BOOTTIME` domain without network setup. Fake multi-node groups use explicit software endpoints and host clock mapping.

Each physical node starts a private `chronyd -x` process under the application account with a private configuration, command socket, and temporary directory. The reference node serves group time on the configured unprivileged clock port; followers estimate their offset to that reference. The runtime combines this estimate with the realtime-to-boot-time mapping to translate sensor timestamps. It never steps or slews the system clock, edits global chrony configuration, or installs a privileged helper. Clock validity depends on freshness and mapping uncertainty, with the configured 0.1 ms default threshold separate from phase readiness thresholds.

Acquisition starts independently of network discovery and clock convergence. Frame reads continue through synchronization startup and clock, transport, or phase failure; status reports the synchronization fault and clears readiness. A packet with an unavailable clock mapping carries `clock_valid: false` and `timestamp_ns: 0`, retaining its local sensor timestamp and pixels. A phase fault can coexist with a valid clock mapping. Applications decide whether synchronization quality permits using those frames. An explicit stop and start establishes a new synchronization run.

Shutdown releases only the runtime's private clock process, temporary files, discovery channel, and transient NetworkManager activation. Changing the reference unit takes effect in a new session without an operator preparation or restoration step. NetworkManager activation follows the application's existing system policy.

## Backend layering

Acquisition configuration, backend/platform profiles, startup gain calibration, frame metadata, and optional developer-host management belong to the managed access layer. The synchronization core consumes only timing snapshots and returns phase-control decisions; it does not read acquisition configuration files, meter images, manage camera brightness, or depend on a developer host. `timing.hpp` defines that boundary separately from acquisition configuration and image types.

Arducam platform profiles own the fixed 5120×800 geometry and four-view layout, with an 8-bit sensor, R8 acquisition, and grayscale output. Raspberry Pi 4 selects the VC4 configuration, while Pi 5 uses the default pipeline. Keeping a single capture path avoids platform-specific pixel conversions and keeps gain calibration and application interfaces uniform. User configuration selects one top-level group frame rate and per-unit platform profile, board-wide exposure, and gain policy. Following the BAPD8 startup practice, automatic gain meters the four views with trimmed means and their median, adjusts sensor analogue gain toward 96 with a 10 percent deadband, and locks after three settled half-second observations. The selected gain range is 1×–8× intersected with sensor limits, and startup has a 20-second settling deadline. Exposure remains fixed. Calibration finishes before timing observations enter phase synchronization; requested and reported exposure/gain remain acquisition metadata, outside core timing types.

A developer workstation optionally manages a group through node-local access services. The independently installable `bcamsync_host` package uses only the Python standard library, inspects the shared synchronization contract, starts nodes, and reads status; synchronization coordination remains on the selected reference unit's node. The host connection does not own session lifetime, and deployment can use local applications without any host management service. The management interface uses SSH requests to local Unix sockets, reusing node-local Session operations. Its shared contract contains group identity, membership, reference unit, backend, clock domain, and group frame rate; local capture and buffering settings are excluded. A start-operation token limits partial-start cleanup to sessions started by that request.

The library provides a common programming model for nodes, synchronization units, camera views, synchronization groups, and acquired frames. Applications initialize and use these objects through B-CamSync. Backend implementations map concrete camera SDKs, drivers, and platform facilities to that model. Arducam is the first implementation target.

The shared layer owns topology, group membership, reference selection, synchronization estimation and policy, and public lifecycle and frame-access semantics. A backend owns device discovery and binding, device configuration, acquisition, pixel buffers and view layout, timestamp interpretation, and execution and completion reporting of synchronization controls. Existing acquisition systems can implement the same acquisition boundary when that integration mode is introduced.

The backend boundary must preserve timing meaning and control completion semantics, along with frame identity, pixel format, dimensions, strides, and buffer lifetime. Camera-specific controls are expressed through explicit backend configuration. Available controls and timing capabilities are described by the backend.

Camera type and host platform are separate concerns. A backend implementation can target a specific SDK and platform combination, while the public topology model stays the same. The implementation uses `AcquisitionBackend` internally and supplies an Arducam/libcamera implementation for Raspberry Pi. Fixture injection remains internal to software tests. Additional backend selection and device-specific configuration will be introduced with the next concrete backend.

Group clock mapping and inter-node transport belong to the shared runtime boundary, with platform-specific mechanisms supplied as needed. Each camera backend supplies its timestamp domain and mapping information rather than reimplementing the network synchronization policy. Synchronization topology is independent of whether applications consume images locally or arrange remote image delivery.

The core, managed acquisition runtime, and initial Arducam backend use C++. Backend responsibilities remain independent of public language bindings. Additional camera backends can be added when concrete devices are introduced.

An explicitly selected fake backend exercises the managed public API in software. It supplies asynchronous board frames and timing observations, configurable synthetic geometry, initial phase, and drift, and the same bounded actuator and image-ownership rules. This makes complete session, binding, and packaging validation independent of camera availability. Fake groups skip physical node provisioning and clock adjustment; their observations use host clocks. Hardware-specific compile checks and device measurements remain separate.

## Language interfaces

The user-facing interfaces are C++ and Python, sharing one C++ synchronization and acquisition implementation, frame ownership rules, and lifecycle semantics. A public C API is outside the intended scope. pybind11 supplies the Python interface and releases the GIL during blocking operations and session destruction. Readonly NumPy views retain native storage through an independent shared owner. CMake exports `BCamSync::bcamsync`; scikit-build-core packages the Python interface and native library together.

Development and CI share a Debian 12 container and executable build entry points. GCC 12 is the normal build and SDK compatibility baseline; Clang 16 supplies supplementary compilation, static analysis, sanitizers, and libFuzzer. Native ARM64 Linux containers align development with the node architecture and Bookworm userspace while keeping the SDK and Python dependencies explicit. Source distributions remain the deployment path; container-built binary packages also exercise installation and language interfaces. Real-device installations build against their deployed vendor SDK.

## Memory safety and validation

Resource ownership is explicit: device handles, mappings, requests, buffers, and threads have scoped owners. Frame objects retain their pixel storage for as long as any C++ frame or Python array view refers to it. Device shutdown and session destruction must preserve outstanding frame storage until its last consumer releases it. Driver buffer reuse and library buffer-pool reuse are separate, defined lifecycle boundaries. Low-level pointer access is concentrated in device and binding boundaries; core observations and decisions use owned value types.

Native development uses strong compiler diagnostics and static analysis, plus separate [AddressSanitizer](https://clang.llvm.org/docs/AddressSanitizer.html)/[UndefinedBehaviorSanitizer](https://clang.llvm.org/docs/UndefinedBehaviorSanitizer.html) and [ThreadSanitizer](https://clang.llvm.org/docs/ThreadSanitizer.html) builds. The address/undefined-behavior build checks out-of-bounds access, invalid frees, use-after-free and scope, alignment, and arithmetic undefined behavior, with leak detection where supported. The thread build checks data races in acquisition, buffer reuse, state publication, and shutdown. These are separate build configurations and apply to the native library and relevant validation executables; Python binding lifecycle checks also instrument the extension and configure the sanitizer runtime for the Python process.

Behavioral validation targets buffer ownership and concurrency: retained frames during ongoing capture; retained NumPy slices after parent frame release; frames surviving session closure; final-reference buffer release; repeated start/stop and partial-start cleanup; slow consumers and pool exhaustion; and shutdown overlapping acquisition or correction. Controlled software acquisition fixtures exercise these paths without a camera, and ARM64 device checks exercise the driver integration. Instrumented execution validates memory and concurrency behavior; release builds are used for throughput and timing measurements.

Fuzzing targets timing observations, control-message parsing, and lifecycle sequences, combined with sanitizers. MemorySanitizer can supplement isolated components when their dependencies can be fully instrumented. Sanitizers observe executed paths; tests must also check that retained frame contents remain unchanged before ownership is released, including cases where pool storage is still allocated and an unintended overwrite would not be reported as use-after-free. Driver DMA ownership and SDK interactions require explicit lifecycle review and device checks.

These checks form the native validation scope requested for this project. Continuous integration exercises software acquisition and ownership independently of physical-device validation.

## First implementation completion criteria

The first implementation must be usable for a complete real-device workflow. It includes a buildable and installable C++ library and Python interface, an Arducam acquisition backend, multi-unit topology configuration, centralized ongoing phase control, node networking and clock setup, local frame consumption, and orderly shutdown. User examples and device validation tools use B-CamSync's own public interfaces and do not depend on BAPD8's acquisition application.

The intended topology coverage comprises eight cameras across two nodes with one board each, eight cameras on one node with two boards, and sixteen cameras across two nodes with two boards each. The first real-device baseline matches the current BAPD8 deployment: two Raspberry Pi 4 nodes, one B0267 four-camera board per node, Raspberry Pi OS Bookworm arm64, and the compatible installed Arducam/libcamera stack. Real-camera runs are performed by the user on the target nodes. The library includes examples and operator-run validation tools for that workflow, while topology configuration supports multiple units per node.

Device validation exercises installation, transient Ethernet link activation and discovery for multi-node configurations, clock convergence, startup phase calibration, local delivery of every configured camera view, continuous phase tracking, correction and restoration, and repeated shutdown and restart. Reference selection is changed between sessions to exercise the same topology with a different master unit. Software ownership and sanitizer checks accompany the implementation. Runtime throughput, synchronization-window metrics, and optical observations are recorded as distinct measurements using release builds where timing is measured.

The board-level frame packet, bounded image buffering, and operator-run device validation are implemented. Synchronization failures remain visible in group status while local acquisition continues; camera failures remain acquisition errors. Restarting synchronization is explicit. Normal group shutdown is initiated by the coordinator and confirmed after follower cleanup. Default capture settings follow the initial board format, while operator configuration supplies device IDs and capture parameters. The device tool defaults to ten-minute acquisition runs and records timestamp-control quality separately from optical results. Successful device checks are reported with the hardware and results actually exercised.

## Implementation background

The starting implementation reference is the RPi synchronization code in the sibling `bapd8_acquisition` repository. It coordinates two Pi 4 nodes, each managing a B0267 synchronization board and four cameras. Reported optical observations were below 0.25 ms and typically around 0.15 ms.

That implementation provides phase estimation and control in `rpi/phase_sync.py`, bounded correction and automatic restoration in `rpi/native/phase_actuator.hpp`, and sensor timing and frame-period actuation through libcamera in `rpi/native/capture_encode.cpp`. Runtime coordination and synchronization evidence are integrated with the acquisition application there; their reusable behavior will be encapsulated behind B-CamSync's camera interface.

## Current design status

The first implementation provides a native timing core, managed acquisition runtime, Arducam backend, C++ and Python interfaces, transient direct-link discovery and private clock mapping, configuration examples, and operator-run device validation. The reference node hosts coordination and clock service. Software checks use acquisition fixtures and instrumented native builds; compilation and acquisition against the installed vendor SDK are performed on the target nodes. External acquisition integration and additional concrete backends remain later extensions of the same acquisition boundary.
