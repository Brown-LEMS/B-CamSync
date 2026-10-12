# B-CamSync

High-precision software synchronization for distributed cameras, combining network clock synchronization with closed-loop frame-phase control.

B-CamSync (B = Brown) is being developed as a reusable synchronization library for Brown lab projects and external researchers. Network-synchronized clocks provide a common time domain; measured frame timing guides bounded frame-period adjustments to align camera phases and correct drift. The goal is sub-millisecond synchronization with explicit quality reporting, subject to device capabilities and physical validation.

## Background and scope

The starting reference is the Raspberry Pi implementation in `bapd8_acquisition`, a multi-camera acquisition project for naturalistic activity in nursing-home environments. Its existing synchronization path coordinates two Pi 4 nodes, each managing a B0267 synchronization board and four cameras. Reported optical observations were below 0.25 ms and typically around 0.15 ms in the observed experiments; these results describe that setup, not a general library guarantee or arbitrary-node validation.

B-CamSync will provide reusable synchronization components while BAPD8 retains its acquisition application. Recording policy, privacy controls, data retention, segmentation, and uploads remain application responsibilities.

This repository currently contains project guidance only. No library code has been extracted yet, and the architecture, public API, packaging, and hardware support are not finalized. Development follows the code-first, test-backed workflow in [AGENTS.md](AGENTS.md); standalone documentation is reserved for consequential design decisions.
