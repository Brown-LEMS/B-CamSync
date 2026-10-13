#pragma once
#include "bcamsync/types.hpp"
#include <array>
#include <optional>
#include <stdexcept>

namespace bcamsync {
struct PhasePolicy {
    double window_seconds = 2.0, evaluation_seconds = 1.0, freshness_seconds = 1.0;
    double deadband_ms = 0.25, ready_p95_ms = 0.5, ready_max_ms = 1.0;
    int ready_windows = 2, startup_delta_us = 1000, startup_frames = 16;
    int tracking_delta_us = 200, tracking_frames = 8;
    double correction_interval_seconds = 2.0, settling_seconds = 0.25,
           application_delay_seconds = 0.25;
    double startup_timeout_seconds = 30.0;
    int startup_max_corrections = 5;
    double excursion_ms = 2.0, excursion_timeout_seconds = 10.0;
    int no_progress_corrections = 3;
    double progress_ms = 0.05, gain = 0.8;
    std::int64_t max_mapping_span_ns = 100000;
    std::size_t max_samples = 512;
    void validate() const;
};
struct Measurement {
    std::int64_t window_start_ns = 0, window_end_ns = 0;
    std::size_t pair_count = 0, master_frames = 0, slave_frames = 0;
    std::size_t unmatched_master = 0, unmatched_slave = 0;
    double phase_ns = 0, absolute_p95_ns = 0, absolute_max_ns = 0;
    double master_period_ns = 0, slave_period_ns = 0;
    double drift_ns_per_second = 0, predicted_phase_ns = 0;
    std::optional<double> correction_progress_ns;
    int ready_windows = 0;
    bool excursion = false;
};
class TimingError : public std::runtime_error {
  public:
    using std::runtime_error::runtime_error;
};
class WindowPending : public TimingError {
  public:
    using TimingError::TimingError;
};
/// Validate freshness, mapping uncertainty, sequence continuity, and monotone timing.
void validate_timing(const TimingSnapshot& snapshot, std::int64_t now_ns,
                     const PhasePolicy& policy = {});
/// Compare an overlapping settled window; no acceptance threshold prefilters pairs.
Measurement estimate_phase(const TimingSnapshot& master, const TimingSnapshot& slave,
                           std::int64_t now_ns, const PhasePolicy& policy = {},
                           std::int64_t after_ns = 0, std::optional<double> previous_phase_ns = {},
                           std::int64_t nominal_period_ns = 25000000);
struct PhaseDecision {
    std::string state;
    bool ready = false;
    std::optional<Pulse> action;
    std::optional<Measurement> measurement;
    std::string fault;
};
/// Deterministic controller: caller supplies common and monotonic clock readings.
class PhaseController {
  public:
    explicit PhaseController(PhasePolicy policy = {}, std::int64_t nominal_period_ns = 25000000);
    void reset(std::optional<std::int64_t> monotonic_ns = {}, std::int64_t after_ns = 0);
    PhaseDecision tick(const TimingSnapshot& master, const TimingSnapshot& slave,
                       std::int64_t now_ns, std::int64_t monotonic_ns, bool clock_valid = true);
    bool ready() const { return ready_; }

  private:
    PhaseDecision result(std::optional<Measurement> measurement = {},
                         std::optional<Pulse> action = {}) const;
    PhaseDecision fail(const std::string& reason, std::optional<Measurement> measurement = {});
    PhasePolicy policy_;
    std::int64_t nominal_period_ns_;
    std::uint64_t next_id_ = 1;
    bool ready_ = false;
    std::string state_, fault_;
    std::optional<std::int64_t> started_, last_tick_, last_eval_, last_start_, excursion_start_;
    std::int64_t after_ = 0, good_end_ = 0;
    std::optional<Pulse> pending_;
    std::optional<Measurement> previous_;
    std::optional<double> before_correction_;
    int good_ = 0, attempts_ = 0, no_progress_ = 0;
    struct Latest {
        std::uint64_t sequence;
        std::int64_t timestamp, received;
    };
    std::array<std::optional<Latest>, 2> latest_;
};
} // namespace bcamsync
