# B-CamSync

Synchronized frame acquisition for distributed cameras.

B-CamSync (B = Brown) is an independent camera library for Brown lab projects and external researchers. Initialize your cameras through the library, then obtain synchronized frames through the same interface. B-CamSync handles camera drivers, frame timing, and synchronization.

The goal is sub-millisecond synchronization with explicit quality reporting. Your application controls how the frames are processed, recorded, and stored.

## Development status

The library is in initial development. No library code has been extracted yet; installation instructions, executable examples, and supported hardware will be documented as implementation becomes available.

See the [design](docs/design.md) for the library's scope and implementation background.
