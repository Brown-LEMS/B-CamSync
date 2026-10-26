#pragma once
#include <cstddef>
#include <cstdint>

namespace bcamsync {
/// Application configuration for synchronization control and readiness.
struct PhasePolicy {
    double window_seconds = 2.0, evaluation_seconds = 1.0, freshness_seconds = 1.0;
    double deadband_ms = 0.25, ready_p95_ms = 0.5, ready_max_ms = 1.0;
    int ready_windows = 2, startup_delta_us = 1000, startup_frames = 16;
    int tracking_delta_us = 200, tracking_frames = 8;
    double correction_interval_seconds = 2.0, settling_seconds = 0.25,
           application_delay_seconds = 0.25;
    int startup_max_corrections = 5;
    double excursion_ms = 2.0, excursion_timeout_seconds = 10.0;
    int no_progress_corrections = 3;
    double progress_ms = 0.05, gain = 0.8;
    std::int64_t max_mapping_span_ns = 100000;
    std::size_t max_samples = 512;
    void validate() const;
};
} // namespace bcamsync
