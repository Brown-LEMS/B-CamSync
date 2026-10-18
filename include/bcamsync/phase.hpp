#pragma once
#include "bcamsync/timing.hpp"
#include "bcamsync/phase_policy.hpp"
#include <array>
#include <optional>
#include <stdexcept>

namespace bcamsync {
// Advanced algorithm interface; applications normally use SyncSession.
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
    ~PhaseController();
    PhaseController(const PhaseController&);
    PhaseController& operator=(const PhaseController&);
    PhaseController(PhaseController&&) noexcept;
    PhaseController& operator=(PhaseController&&) noexcept;
    bool ready() const;

  private:
    void* handle_ = nullptr;
};
} // namespace bcamsync
