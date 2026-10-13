#pragma once
#include "backend.hpp"

namespace bcamsync {
/// Synthetic sensor phase and frequency relative to the node clock.
struct FakeOptions {
    std::int64_t initial_phase_us = 0;
    double drift_ppm = 0;
};
/// Autonomous synthetic acquisition using the real queue, actuator, and synchronization runtime.
std::vector<std::unique_ptr<AcquisitionBackend>>
make_fake_backends(const std::vector<UnitConfig>& units);
/// Explicit options must contain one entry for each unit.
std::vector<std::unique_ptr<AcquisitionBackend>>
make_fake_backends(const std::vector<UnitConfig>& units, const std::vector<FakeOptions>& options);
} // namespace bcamsync
