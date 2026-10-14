#pragma once
#include <cstdint>
#include <string>
#include <vector>

namespace bcamsync {
struct FrameTiming {
    std::uint64_t camera_sequence = 0;
    std::int64_t sensor_timestamp_ns = 0, timestamp_ns = 0, mapping_span_ns = 0;
    std::int64_t requested_duration_us = 0, reported_duration_us = 0;
    std::uint64_t correction_id = 0;
};
struct CorrectionStatus {
    std::string state = "idle";
    std::uint64_t correction_id = 0;
    std::int64_t restored_timestamp_ns = 0;
};
struct TimingSnapshot {
    std::string unit_id;
    std::int64_t snapshot_timestamp_ns = 0;
    std::vector<FrameTiming> frames;
    CorrectionStatus correction;
};
struct Pulse {
    std::uint64_t correction_id = 0;
    int delta_us = 0, count = 0;
    std::int64_t expires_ns = 0;
};
/// Node-local sensor clock for adapters; CLOCK_BOOTTIME on Linux.
std::int64_t boot_time_ns();
std::int64_t monotonic_ns();
std::int64_t realtime_ns();
} // namespace bcamsync
