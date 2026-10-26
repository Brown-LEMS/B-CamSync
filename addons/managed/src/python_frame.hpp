#pragma once

#include "bcamsync/types.hpp"
#include <memory>

namespace bcamsync::python {
// Python packet objects and all NumPy views retain the immutable native packet.
struct FrameHandle {
    std::shared_ptr<const FramePacket> packet;
};
}
