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
| Eight cameras, two nodes, one board per node | Two boards | Align node clocks over the network | One reference and one follower |
| Eight cameras, one node, two boards | Two boards | Shared local clock domain | One reference and one follower |
| Sixteen cameras, two nodes, two boards per node | Four boards | Align node clocks over the network | One reference and three followers |

Clock alignment and frame-phase alignment are separate layers. Units on the same node use a shared local time domain and still require ongoing frame-phase observation and correction. Units on different nodes additionally require network clock alignment and timestamp mapping into a common domain. For the current libcamera path, sensor timestamps use the node's `CLOCK_BOOTTIME` domain.

Closed-loop frame-phase control measures phase differences and drift, applies bounded frame-period adjustments, and restores the baseline period after each correction. Local and remote units share the same estimation and control model; access to their observations and controls is local or networked according to placement.

Synchronization runs continuously after startup calibration, independently of application frame consumption, processing, recording, and persistence. The library reports synchronization state and quality; applications decide how those reports affect their own workflows.

The goal is sub-millisecond synchronization with explicit quality reporting. Synchronization observations and control state should be available alongside frame acquisition so applications can interpret the timing of their data.

## Node clock and network management

B-CamSync manages node clock preparation, startup alignment, readiness checks, and ongoing health observation. The initial Raspberry Pi implementation uses chrony, with deployment and privileged node-service operations handling system configuration and initial clock adjustment. Camera applications use the library without manually generating chrony configuration or invoking calibration commands.

For a group spanning nodes, the reference node serves the common clock and other nodes synchronize to it. Group-relative time is sufficient for camera synchronization; an upstream UTC source is an explicit option for applications that need it. Initial clock stepping is completed before capture and calibration. During capture, ongoing clock correction is gradual. Units colocated on one node share its local time domain and do not require network clock configuration to synchronize with each other.

Network and IP management are node-level platform responsibilities alongside clock management, separate from camera backends and synchronization algorithms. The library must support two nodes connected directly by Ethernet, with no external switch, router, or DHCP service. Configuration identifies the synchronization interface and associates stable node identities with reachable addresses. Clock-service and control-channel configuration use the same resolved topology.

Network addressing is independent of the selected reference unit. Selecting another reference changes coordinator and clock-source roles while preserving node identity and address assignment. Node services provision and activate the synchronization network before clock alignment; frame-phase calibration follows clock readiness.

The first direct-link implementation uses a dedicated static IPv4 subnet and a persistent platform network profile per node, with no gateway or DNS on the synchronization link. Node identity, synchronization interface, and subnet configuration determine the node addresses and the corresponding clock and control endpoints. Addresses are assigned to nodes and remain stable across reference selection. The Raspberry Pi OS Bookworm platform implementation uses NetworkManager. Deployments with an already configured network can explicitly supply their existing addresses. Detailed address allocation and profile lifecycle remain technical decisions to finalize.

## Backend layering

The library provides a common programming model for nodes, synchronization units, camera views, synchronization groups, and acquired frames. Applications initialize and use these objects through B-CamSync. Backend implementations map concrete camera SDKs, drivers, and platform facilities to that model. Arducam is the first implementation target.

The shared layer owns topology, group membership, reference selection, synchronization estimation and policy, and public lifecycle and frame-access semantics. A backend owns device discovery and binding, device configuration, acquisition, pixel buffers and view layout, timestamp interpretation, and execution and completion reporting of synchronization controls. Existing acquisition systems can implement the same acquisition boundary when that integration mode is introduced.

The backend boundary must preserve timing meaning and control completion semantics, along with frame identity, pixel format, dimensions, strides, and buffer lifetime. Camera-specific controls are expressed through explicit backend configuration. Available controls and timing capabilities are described by the backend.

Camera type and host platform are separate configuration concerns. A backend implementation can target a specific SDK and platform combination, while the public topology model stays the same. The initial target is the Arducam stack on Raspberry Pi; backend naming and concrete configuration types remain to be defined.

Node clock alignment and inter-node transport belong to the shared runtime boundary, with platform-specific mechanisms supplied as needed. Each camera backend supplies its timestamp domain and mapping information rather than reimplementing the network synchronization policy. Synchronization topology is independent of whether applications consume images locally or arrange remote image delivery.

The core, managed acquisition runtime, and initial Arducam backend will use C++. Backend responsibilities remain independent of public language bindings. Start with the Arducam implementation and a clear backend contract; additional backends can be added when concrete devices are introduced.

## Language interfaces

The user-facing interfaces are C++ and Python, sharing one C++ synchronization and acquisition implementation, frame ownership rules, and lifecycle semantics. A public C API is outside the intended scope. Python binding technology remains to be selected. The C++ implementation direction is accompanied by explicit memory-safety design and validation requirements.

## Memory safety and validation

Resource ownership is explicit: device handles, mappings, requests, buffers, and threads have scoped owners. Frame objects retain their pixel storage for as long as any C++ frame or Python array view refers to it. Device shutdown and session destruction must preserve outstanding frame storage until its last consumer releases it. Driver buffer reuse and library buffer-pool reuse are separate, defined lifecycle boundaries. Low-level pointer access is concentrated in device and binding boundaries; core observations and decisions use owned value types.

Native development uses strong compiler diagnostics and static analysis, plus separate [AddressSanitizer](https://clang.llvm.org/docs/AddressSanitizer.html)/[UndefinedBehaviorSanitizer](https://clang.llvm.org/docs/UndefinedBehaviorSanitizer.html) and [ThreadSanitizer](https://clang.llvm.org/docs/ThreadSanitizer.html) builds. The address/undefined-behavior build checks out-of-bounds access, invalid frees, use-after-free and scope, alignment, and arithmetic undefined behavior, with leak detection where supported. The thread build checks data races in acquisition, buffer reuse, state publication, and shutdown. These are separate build configurations and apply to the native library and relevant validation executables; Python binding lifecycle checks also instrument the extension and configure the sanitizer runtime for the Python process.

Behavioral validation targets buffer ownership and concurrency: retained frames during ongoing capture; retained NumPy slices after parent frame release; frames surviving session closure; final-reference buffer release; repeated start/stop and partial-start cleanup; slow consumers and pool exhaustion; and shutdown overlapping acquisition or correction. Controlled software acquisition fixtures exercise these paths without a camera, and ARM64 device checks exercise the driver integration. Instrumented execution validates memory and concurrency behavior; release builds are used for throughput and timing measurements.

Fuzzing targets timing observations, control-message parsing, and lifecycle sequences, combined with sanitizers. MemorySanitizer can supplement isolated components when their dependencies can be fully instrumented. Sanitizers observe executed paths; tests must also check that retained frame contents remain unchanged before ownership is released, including cases where pool storage is still allocated and an unintended overwrite would not be reported as use-after-free. Driver DMA ownership and SDK interactions require explicit lifecycle review and device checks.

These checks are introduced with implementation and form the native validation scope requested for this project. No placeholder test framework or device experiments are introduced while the repository contains design only.

## First implementation completion criteria

The first implementation must be usable for a complete real-device workflow. It includes a buildable and installable C++ library and Python interface, an Arducam acquisition backend, multi-unit topology configuration, centralized ongoing phase control, node networking and clock setup, local frame consumption, and orderly shutdown. User examples and device validation tools use B-CamSync's own public interfaces and do not depend on BAPD8's acquisition application.

The intended topology coverage comprises eight cameras across two nodes with one board each, eight cameras on one node with two boards, and sixteen cameras across two nodes with two boards each. Real-device validation is performed on the supplied hardware; the available Pi models, board and camera variants, installed SDK/driver stack, and device access must be identified before backend implementation is finalized.

Device validation exercises installation, Ethernet direct-link preparation for multi-node configurations, clock convergence, startup phase calibration, local delivery of every configured camera view, continuous phase tracking, correction and restoration, and repeated shutdown and restart. Reference selection is changed between sessions to exercise the same topology with a different master unit. Software ownership and sanitizer checks accompany the implementation. Runtime throughput, synchronization-window metrics, and optical observations are recorded as distinct measurements using release builds where timing is measured.

Frame delivery granularity, slow-consumer behavior, behavior on synchronization or network failure, capture settings, and acceptance duration and thresholds remain to be finalized. These affect the public contract and the device acceptance workload. Implementation is complete when the library and deployment flow are ready for the requested device checks; successful device checks are reported separately with the hardware and results actually exercised.

## Implementation background

The starting implementation reference is the RPi synchronization code in the sibling `bapd8_acquisition` repository. It coordinates two Pi 4 nodes, each managing a B0267 synchronization board and four cameras. Reported optical observations were below 0.25 ms and typically around 0.15 ms.

That implementation provides phase estimation and control in `rpi/phase_sync.py`, bounded correction and automatic restoration in `rpi/native/phase_actuator.hpp`, and sensor timing and frame-period actuation through libcamera in `rpi/native/capture_encode.cpp`. Runtime coordination and synchronization evidence are integrated with the acquisition application there; their reusable behavior will be encapsulated behind B-CamSync's camera interface.

## Current design status

The independent library boundary, synchronization topology model, centralized computation with a freely selectable reference unit, core/integration separation, and multi-backend direction are established. Both managed and external acquisition are intended integration modes; the first implementation provides C++ managed acquisition for Arducam on Raspberry Pi with C++ and Python interfaces, local frame consumption, inter-node synchronization communication, and memory-safety validation. The reference node hosts coordination and clock service; node-level clock and network management include direct Ethernet deployment. Detailed API signatures, backend contracts, network address allocation and profile lifecycle, internal runtime architecture, binding technology, packaging, and supported device/platform combinations remain to be decided. This repository currently contains project guidance and this design document; library code has not yet been extracted.
