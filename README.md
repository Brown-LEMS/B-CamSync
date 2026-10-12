# B-CamSync

An independent camera library that provides synchronized frame acquisition above camera drivers.

B-CamSync (B = Brown) is being developed for Brown lab projects and external researchers. Users initialize cameras through the library and obtain frames through the same interface. The library owns camera access and synchronization, encapsulating driver interaction, timestamp handling, and frame-phase control so applications can use synchronized cameras without managing those mechanisms themselves.

Network-synchronized clocks provide a common time domain; measured frame timing guides bounded frame-period adjustments to align camera phases and correct drift. The goal is sub-millisecond synchronization with explicit quality reporting.

## Background and scope

The starting implementation reference is the Raspberry Pi synchronization code in `bapd8_acquisition`. That system uses two Pi 4 nodes, each managing a B0267 synchronization board and four cameras, with reported optical synchronization errors below 0.25 ms and typically around 0.15 ms.

B-CamSync is developed and maintained independently, with its interfaces shaped by reusable camera initialization and frame acquisition. BAPD8's application scenario does not determine the library's design. BAPD8 continues to own its recording, privacy, retention, segmentation, and data-transfer behavior in its own repository.

This repository currently contains project guidance only. No library code has been extracted yet, and the architecture, public API, packaging, and hardware support are not finalized. Development follows the code-first, test-backed workflow in [AGENTS.md](AGENTS.md); standalone documentation is reserved for consequential design decisions.
