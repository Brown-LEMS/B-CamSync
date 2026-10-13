#include "bcamsync/phase.hpp"
#include <algorithm>
#include <cmath>
#include <limits>
#include <numeric>

namespace bcamsync {
namespace {
std::int64_t ns(double seconds) { return static_cast<std::int64_t>(std::llround(seconds * 1e9)); }
double median(std::vector<double> values) {
    std::sort(values.begin(), values.end());
    const auto n = values.size();
    return n % 2 ? values[n / 2] : (values[n / 2 - 1] + values[n / 2]) / 2;
}
void check_frames(const TimingSnapshot& snapshot, std::int64_t now, const PhasePolicy& policy) {
    const auto& frames = snapshot.frames;
    if (frames.empty())
        throw WindowPending("empty timing window");
    if (frames.size() > policy.max_samples)
        throw TimingError("timing window exceeds sample bound");
    if (now <= 0 || snapshot.snapshot_timestamp_ns <= 0)
        throw TimingError("invalid common timestamp");
    const auto latest = frames.back().timestamp_ns;
    if (latest <= 0 || latest > snapshot.snapshot_timestamp_ns ||
        now - latest > ns(policy.freshness_seconds) || latest - now > policy.max_mapping_span_ns)
        throw TimingError("stale or future timing evidence");
    for (std::size_t i = 0; i < frames.size(); ++i) {
        const auto& f = frames[i];
        if (f.timestamp_ns <= 0 || f.sensor_timestamp_ns <= 0)
            throw TimingError("invalid frame timestamp");
        if (f.mapping_span_ns < 0 || f.mapping_span_ns > policy.max_mapping_span_ns)
            throw TimingError("clock mapping uncertainty exceeds bound");
        if (!i)
            continue;
        const auto& a = frames[i - 1];
        if (a.camera_sequence == std::numeric_limits<std::uint64_t>::max() ||
            f.camera_sequence != a.camera_sequence + 1)
            throw TimingError("camera sequence discontinuity");
        if (f.timestamp_ns <= a.timestamp_ns || f.sensor_timestamp_ns <= a.sensor_timestamp_ns)
            throw TimingError("nonmonotone camera timing");
    }
}
} // namespace
void PhasePolicy::validate() const {
    for (const auto value :
         {window_seconds, evaluation_seconds, freshness_seconds, deadband_ms, ready_p95_ms,
          ready_max_ms, correction_interval_seconds, settling_seconds, application_delay_seconds,
          excursion_ms, excursion_timeout_seconds, progress_ms, gain})
        if (!std::isfinite(value) || value <= 0 || value > 1e9)
            throw std::invalid_argument(
                "phase policy values must be finite and positive within timestamp range");
    for (const auto value : {ready_windows, startup_delta_us, startup_frames, tracking_delta_us,
                             tracking_frames, startup_max_corrections, no_progress_corrections})
        if (value <= 0)
            throw std::invalid_argument("phase policy counts must be positive");
    if (max_mapping_span_ns <= 0 || !max_samples || max_samples > 512 || gain > 1 ||
        window_seconds > 6)
        throw std::invalid_argument("phase policy exceeds controller bounds");
    if (startup_delta_us > 1000 || startup_frames > 16 || tracking_delta_us > 200 ||
        tracking_frames > 8)
        throw std::invalid_argument("phase pulse exceeds hard bounds");
    if (!(deadband_ms <= ready_p95_ms && ready_p95_ms <= ready_max_ms &&
          ready_max_ms < excursion_ms))
        throw std::invalid_argument("phase thresholds must be ordered");
}
void validate_timing(const TimingSnapshot& snapshot, std::int64_t now, const PhasePolicy& policy) {
    policy.validate();
    check_frames(snapshot, now, policy);
}
Measurement estimate_phase(const TimingSnapshot& master, const TimingSnapshot& slave,
                           std::int64_t now, const PhasePolicy& policy, std::int64_t after,
                           std::optional<double> previous, std::int64_t nominal) {
    policy.validate();
    if (nominal <= 0 || after < 0 || (previous && !std::isfinite(*previous)))
        throw std::invalid_argument("invalid phase estimation arguments");
    check_frames(master, now, policy);
    check_frames(slave, now, policy);
    Measurement m;
    m.window_end_ns = std::min(master.frames.back().timestamp_ns, slave.frames.back().timestamp_ns);
    m.window_start_ns = m.window_end_ns - ns(policy.window_seconds);
    if (m.window_start_ns < after ||
        std::max(master.frames.front().timestamp_ns, slave.frames.front().timestamp_ns) >
            m.window_start_ns)
        throw WindowPending("insufficient settled overlapping coverage");
    std::array<std::vector<const FrameTiming*>, 2> selected;
    std::array<double, 2> periods{};
    const std::array<const TimingSnapshot*, 2> snapshots{{&master, &slave}};
    for (std::size_t role = 0; role < 2; ++role) {
        auto& frames = selected[role];
        for (const auto& f : snapshots[role]->frames)
            if (f.timestamp_ns >= m.window_start_ns && f.timestamp_ns <= m.window_end_ns)
                frames.push_back(&f);
        if (static_cast<double>(frames.size()) <
            std::max(3.0,
                     std::floor(policy.window_seconds * 1e9 / static_cast<double>(nominal) * 0.9)))
            throw TimingError("insufficient frame coverage");
        std::vector<double> intervals;
        for (std::size_t i = 1; i < frames.size(); ++i)
            intervals.push_back(static_cast<double>(frames[i]->sensor_timestamp_ns -
                                                    frames[i - 1]->sensor_timestamp_ns));
        periods[role] = median(intervals);
        if (periods[role] < .9 * static_cast<double>(nominal) ||
            periods[role] > 1.1 * static_cast<double>(nominal) ||
            std::any_of(intervals.begin(), intervals.end(), [&](double v) {
                return v < .8 * periods[role] || v > 1.2 * periods[role];
            }))
            throw TimingError("camera cadence anomaly in " + snapshots[role]->unit_id);
        if (static_cast<double>(frames.front()->timestamp_ns - m.window_start_ns) >
                1.5 * periods[role] ||
            static_cast<double>(m.window_end_ns - frames.back()->timestamp_ns) >
                1.5 * periods[role])
            throw TimingError("inadequate overlapping boundary coverage");
    }
    const auto &left = selected[0], &right = selected[1];
    // Subtract integer epoch timestamps before conversion; floating epochs lose nanoseconds.
    const auto nearest = [&](std::int64_t timestamp, double offset) {
        auto it = std::lower_bound(
            right.begin(), right.end(), offset, [&](const FrameTiming* f, double value) {
                return static_cast<double>(f->timestamp_ns - timestamp) < value;
            });
        std::size_t best =
            it == right.end() ? right.size() - 1 : static_cast<std::size_t>(it - right.begin());
        if (best &&
            std::abs(static_cast<double>(right[best - 1]->timestamp_ns - timestamp) - offset) <=
                std::abs(static_cast<double>(right[best]->timestamp_ns - timestamp) - offset))
            --best;
        return best;
    };
    double anchor = previous.value_or(0);
    if (!previous) {
        const auto timestamp = left[left.size() / 2]->timestamp_ns;
        anchor = static_cast<double>(right[nearest(timestamp, 0)]->timestamp_ns - timestamp);
    }
    std::vector<bool> used(right.size(), false);
    std::vector<double> deltas;
    std::size_t interior = 0;
    for (std::size_t i = 0; i < left.size(); ++i) {
        const auto j = nearest(left[i]->timestamp_ns, std::nearbyint(anchor));
        const auto delta = static_cast<double>(right[j]->timestamp_ns - left[i]->timestamp_ns);
        if (used[j] ||
            std::abs(delta - std::nearbyint(anchor)) > std::max(periods[0], periods[1]) / 2) {
            ++m.unmatched_master;
            if (i && i + 1 < left.size())
                ++interior;
        } else {
            used[j] = true;
            deltas.push_back(delta);
        }
    }
    for (std::size_t i = 0; i < used.size(); ++i)
        if (!used[i]) {
            ++m.unmatched_slave;
            if (i && i + 1 < used.size())
                ++interior;
        }
    if (interior || deltas.empty())
        throw TimingError("unmatched interior frames");
    auto phase = median(deltas);
    const auto period = (periods[0] + periods[1]) / 2;
    if (previous)
        phase += std::nearbyint((*previous - phase) / period) * period;
    std::vector<double> absolute;
    for (auto& delta : deltas) {
        delta += std::nearbyint((phase - delta) / period) * period;
        absolute.push_back(std::abs(delta));
    }
    std::sort(absolute.begin(), absolute.end());
    m.phase_ns = median(deltas);
    m.absolute_p95_ns =
        absolute[static_cast<std::size_t>(std::ceil(.95 * static_cast<double>(absolute.size()))) -
                 1];
    m.absolute_max_ns = absolute.back();
    m.master_period_ns = periods[0];
    m.slave_period_ns = periods[1];
    m.pair_count = deltas.size();
    m.master_frames = left.size();
    m.slave_frames = right.size();
    return m;
}
PhaseController::PhaseController(PhasePolicy policy, std::int64_t nominal)
    : policy_(policy), nominal_period_ns_(nominal) {
    policy_.validate();
    if (nominal <= 0)
        throw std::invalid_argument("nominal frame period must be positive");
    reset();
}
void PhaseController::reset(std::optional<std::int64_t> mono, std::int64_t after) {
    if ((mono && *mono < 0) || after < 0)
        throw std::invalid_argument("negative reset boundary");
    ready_ = false;
    state_ = "warming";
    fault_.clear();
    last_tick_ = mono;
    last_eval_.reset();
    last_start_.reset();
    excursion_start_.reset();
    after_ = after;
    pending_.reset();
    previous_.reset();
    before_correction_.reset();
    good_ = attempts_ = no_progress_ = 0;
    good_end_ = 0;
    latest_ = {};
}
PhaseDecision PhaseController::result(std::optional<Measurement> m,
                                      std::optional<Pulse> action) const {
    return {state_, ready_, action, m, fault_};
}
PhaseDecision PhaseController::fail(const std::string& reason, std::optional<Measurement> m) {
    state_ = "fault";
    ready_ = false;
    fault_ = reason;
    return result(m);
}
PhaseDecision PhaseController::tick(const TimingSnapshot& master, const TimingSnapshot& slave,
                                    std::int64_t now, std::int64_t mono, bool clock_valid) {
    const auto& p = policy_;
    if (!fault_.empty())
        return result();
    if (mono < 0 || (last_tick_ && mono < *last_tick_))
        return fail("monotonic clock moved backwards");
    last_tick_ = mono;
    if (!clock_valid)
        return fail("clock health lost");
    try {
        check_frames(master, now, p);
        check_frames(slave, now, p);
    } catch (const WindowPending&) {
        return ready_ ? fail("missing timing evidence") : result();
    } catch (const TimingError& e) {
        return fail(e.what());
    }
    const std::array<const TimingSnapshot*, 2> snapshots{{&master, &slave}};
    for (std::size_t i = 0; i < 2; ++i) {
        const auto& frame = snapshots[i]->frames.back();
        const auto& previous = latest_[i];
        if (previous) {
            if (frame.camera_sequence < previous->sequence ||
                frame.sensor_timestamp_ns < previous->sensor_timestamp ||
                (frame.camera_sequence == previous->sequence &&
                 frame.sensor_timestamp_ns != previous->sensor_timestamp))
                return fail("camera timing restarted or changed identity");
            if (frame.camera_sequence == previous->sequence) {
                if (mono - previous->received > ns(p.freshness_seconds))
                    return fail("timing evidence stalled on monotonic deadline");
                continue;
            }
        }
        latest_[i] = Latest{frame.camera_sequence, frame.sensor_timestamp_ns, mono};
    }
    if (pending_) {
        const auto& correction = slave.correction;
        if (correction.correction_id != pending_->correction_id)
            return mono - *last_start_ > 5000000000LL ? fail("correction acknowledgement timeout")
                                                      : result();
        if (correction.state != "restored" && correction.state != "cancelled")
            return mono - *last_start_ > 5000000000LL ? fail("correction restoration timeout")
                                                      : result();
        if (correction.restored_timestamp_ns <= 0 || correction.restored_timestamp_ns > now ||
            correction.restored_timestamp_ns >
                std::numeric_limits<std::int64_t>::max() - ns(p.settling_seconds))
            return fail("invalid restoration boundary");
        after_ = correction.restored_timestamp_ns + ns(p.settling_seconds);
        pending_.reset();
        state_ = "settling";
    }
    if (last_eval_ && mono - *last_eval_ < ns(p.evaluation_seconds))
        return result();
    Measurement m;
    try {
        m = estimate_phase(master, slave, now, p, after_,
                           previous_ ? std::optional<double>(previous_->phase_ns) : std::nullopt,
                           nominal_period_ns_);
    } catch (const WindowPending&) {
        return result();
    } catch (const TimingError& e) {
        return fail(e.what());
    }
    if (previous_ && m.window_end_ns <= previous_->window_end_ns)
        return result();
    last_eval_ = mono;
    double drift = 0;
    if (previous_ && !before_correction_)
        drift = (m.phase_ns - previous_->phase_ns) /
                static_cast<double>(m.window_end_ns - previous_->window_end_ns);
    m.drift_ns_per_second = drift * 1e9;
    m.predicted_phase_ns =
        m.phase_ns + drift * (p.window_seconds / 2 + p.application_delay_seconds) * 1e9;
    if (before_correction_) {
        m.correction_progress_ns = *before_correction_ - std::abs(m.phase_ns);
        no_progress_ = std::abs(m.phase_ns) > p.excursion_ms * 1e6 &&
                               *m.correction_progress_ns < p.progress_ms * 1e6
                           ? no_progress_ + 1
                           : 0;
        before_correction_.reset();
    }
    previous_ = m;
    m.excursion = std::abs(m.phase_ns) > p.excursion_ms * 1e6;
    if (m.excursion) {
        if (!excursion_start_)
            excursion_start_ = mono;
        if (mono - *excursion_start_ >= ns(p.excursion_timeout_seconds) ||
            no_progress_ >= p.no_progress_corrections)
            return fail("persistent phase nonconvergence", m);
    } else
        excursion_start_.reset();
    const bool passes =
        m.absolute_p95_ns <= p.ready_p95_ms * 1e6 && m.absolute_max_ns <= p.ready_max_ms * 1e6;
    if (!ready_) {
        if (!passes)
            good_ = 0;
        else if (m.window_start_ns >= good_end_) {
            ++good_;
            good_end_ = m.window_end_ns;
            if (good_ >= p.ready_windows)
                ready_ = true;
        }
    }
    state_ = ready_ ? "tracking" : "calibrating";
    m.ready_windows = good_;
    if (std::max(std::abs(m.phase_ns), std::abs(m.predicted_phase_ns)) <= p.deadband_ms * 1e6)
        return result(m);
    if (last_start_ && mono - *last_start_ < ns(p.correction_interval_seconds))
        return result(m);
    if (!ready_ && attempts_ >= p.startup_max_corrections)
        return fail("startup correction budget exhausted", m);
    const auto bound = ready_ ? p.tracking_delta_us : p.startup_delta_us;
    const auto count = ready_ ? p.tracking_frames : p.startup_frames;
    const auto raw_delta =
        std::nearbyint(-p.gain * m.predicted_phase_ns / (static_cast<double>(count) * 1000));
    const auto delta = static_cast<int>(
        std::clamp(raw_delta, -static_cast<double>(bound), static_cast<double>(bound)));
    if (!delta)
        return result(m);
    if (now > std::numeric_limits<std::int64_t>::max() - 5000000000LL)
        return fail("correction timestamp overflow", m);
    Pulse action{next_id_++, delta, count, now + 5000000000LL};
    pending_ = action;
    last_start_ = mono;
    before_correction_ = std::abs(m.phase_ns);
    ++attempts_;
    good_ = 0;
    good_end_ = 0;
    state_ = "correcting";
    return result(m, action);
}
} // namespace bcamsync
