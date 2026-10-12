# B-CamSync Design

## Library role

B-CamSync (B = Brown) is an independent camera library above camera drivers, intended for Brown lab projects and external researchers. Its primary usage flow is camera initialization followed by frame acquisition through the library. In ordinary use, the application accesses cameras through B-CamSync, which encapsulates the underlying driver and synchronization implementation.

This boundary gives applications one place to manage synchronized camera access. Camera lifecycle, timestamp handling, and synchronization can be coordinated internally, while applications consume frames and synchronization quality information.

## Responsibilities

B-CamSync owns camera initialization, camera access, frame acquisition, timestamp handling, synchronization control, and quality reporting. Its interfaces should follow this usage flow and expose the information applications need to use the resulting frames.

Applications own frame processing, recording policy, privacy controls, retention, segmentation, and data transfer. BAPD8 maintains those responsibilities in its own repository. B-CamSync's design is independent of BAPD8's application scenario.

## Synchronization approach

Network clock synchronization establishes a common time domain. Sensor timestamps are mapped into that domain so frame timing can be compared across nodes. Closed-loop frame-phase control measures phase differences and drift, applies bounded frame-period adjustments, and restores the baseline period after each correction.

The goal is sub-millisecond synchronization with explicit quality reporting. Synchronization observations and control state should be available alongside frame acquisition so applications can interpret the timing of their data.

## Implementation background

The starting implementation reference is the RPi synchronization code in the sibling `bapd8_acquisition` repository. It coordinates two Pi 4 nodes, each managing a B0267 synchronization board and four cameras. Reported optical observations were below 0.25 ms and typically around 0.15 ms.

That implementation provides phase estimation and control in `rpi/phase_sync.py`, bounded correction and automatic restoration in `rpi/native/phase_actuator.hpp`, and sensor timing and frame-period actuation through libcamera in `rpi/native/capture_encode.cpp`. Runtime coordination and synchronization evidence are integrated with the acquisition application there; their reusable behavior will be encapsulated behind B-CamSync's camera interface.

## Current design status

The independent library boundary and initialization-to-frame-acquisition usage flow are established. Detailed API signatures, internal architecture, packaging, and supported hardware will be decided through implementation and explicit user decisions. This repository currently contains project guidance and this design document; library code has not yet been extracted.
