#pragma once
#include "backend.hpp"

namespace bcamsync {
/// Autonomous synthetic acquisition using the real queue, actuator, and synchronization runtime.
std::vector<std::unique_ptr<AcquisitionBackend>>
make_fake_backends(const std::vector<UnitConfig>& units, unsigned fps = 40);
/// Explicit options must contain one entry for each unit.
std::vector<std::unique_ptr<AcquisitionBackend>>
make_fake_backends(const std::vector<UnitConfig>& units, const std::vector<FakeOptions>& options, unsigned fps = 40);
} // namespace bcamsync
