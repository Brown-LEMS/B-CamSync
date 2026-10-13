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

Synchronization computation is centralized in one group coordinator, which maintains separate measurement and correction state for each follower against the same reference. Each node supplies local timing and executes local controls; a complete phase controller is not independently run on every node. The coordinator's placement and the network clock reference are deployment concerns separate from the unit's phase-reference role.

| Configuration | Units | Clock arrangement | Phase arrangement |
| --- | --- | --- | --- |
| Eight cameras, two nodes, one board per node | Two boards | Align node clocks over the network | One reference and one follower |
| Eight cameras, one node, two boards | Two boards | Shared local clock domain | One reference and one follower |
| Sixteen cameras, two nodes, two boards per node | Four boards | Align node clocks over the network | One reference and three followers |

Clock alignment and frame-phase alignment are separate layers. Units on the same node use a shared local time domain and still require ongoing frame-phase observation and correction. Units on different nodes additionally require network clock alignment and timestamp mapping into a common domain. For the current libcamera path, sensor timestamps use the node's `CLOCK_BOOTTIME` domain.

Closed-loop frame-phase control measures phase differences and drift, applies bounded frame-period adjustments, and restores the baseline period after each correction. Local and remote units share the same estimation and control model; access to their observations and controls is local or networked according to placement.

Synchronization runs continuously after startup calibration, independently of application frame consumption, processing, recording, and persistence. The library reports synchronization state and quality; applications decide how those reports affect their own workflows.

The goal is sub-millisecond synchronization with explicit quality reporting. Synchronization observations and control state should be available alongside frame acquisition so applications can interpret the timing of their data.

## Backend layering

The library provides a common programming model for nodes, synchronization units, camera views, synchronization groups, and acquired frames. Applications initialize and use these objects through B-CamSync. Backend implementations map concrete camera SDKs, drivers, and platform facilities to that model. Arducam is the first implementation target.

The shared layer owns topology, group membership, reference selection, synchronization estimation and policy, and public lifecycle and frame-access semantics. A backend owns device discovery and binding, device configuration, acquisition, pixel buffers and view layout, timestamp interpretation, and execution and completion reporting of synchronization controls. Existing acquisition systems can implement the same acquisition boundary when that integration mode is introduced.

The backend boundary must preserve timing meaning and control completion semantics, along with frame identity, pixel format, dimensions, strides, and buffer lifetime. Camera-specific controls are expressed through explicit backend configuration. Available controls and timing capabilities are described by the backend.

Camera type and host platform are separate configuration concerns. A backend implementation can target a specific SDK and platform combination, while the public topology model stays the same. The initial target is the Arducam stack on Raspberry Pi; backend naming and concrete configuration types remain to be defined.

Node clock alignment and inter-node transport belong to the shared runtime boundary, with platform-specific mechanisms supplied as needed. Each camera backend supplies its timestamp domain and mapping information rather than reimplementing the network synchronization policy. Synchronization topology is independent of whether applications consume images locally or arrange remote image delivery.

The backend boundary does not prescribe Python or native implementation. Public API and backend responsibilities are established first; concrete language bindings, threading, and buffer delivery are implementation decisions. Start with the Arducam implementation and a clear backend contract; additional backends can be added when concrete devices are introduced.

## Implementation background

The starting implementation reference is the RPi synchronization code in the sibling `bapd8_acquisition` repository. It coordinates two Pi 4 nodes, each managing a B0267 synchronization board and four cameras. Reported optical observations were below 0.25 ms and typically around 0.15 ms.

That implementation provides phase estimation and control in `rpi/phase_sync.py`, bounded correction and automatic restoration in `rpi/native/phase_actuator.hpp`, and sensor timing and frame-period actuation through libcamera in `rpi/native/capture_encode.cpp`. Runtime coordination and synchronization evidence are integrated with the acquisition application there; their reusable behavior will be encapsulated behind B-CamSync's camera interface.

## Current design status

The independent library boundary, synchronization topology model, centralized computation with a freely selectable reference unit, core/integration separation, and multi-backend direction are established. Both managed and external acquisition are intended integration modes; the first implementation provides managed acquisition for Arducam on Raspberry Pi with local frame consumption and inter-node synchronization communication. Detailed API signatures, backend contracts, coordinator placement, clock provisioning, internal runtime architecture, packaging, and supported device/platform combinations remain to be decided. This repository currently contains project guidance and this design document; library code has not yet been extracted.
