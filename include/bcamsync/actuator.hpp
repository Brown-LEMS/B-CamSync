#pragma once

#include <cstdint>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <string>

namespace bcamsync {
// Request submission and observed completion are separate boundaries.
class PhaseActuator {
public:
    struct Submission { int64_t duration_us = 0; uint64_t correction_id = 0; bool restoration = false; uint64_t round_id = 0; };
    struct State {
        std::string session_id, state = "idle";
        uint64_t round_id = 0, correction_id = 0;
        int delta_us = 0, count = 0, submitted_frames = 0;
        int64_t expires_ns = 0, deadline_mono_ns = 0, restored_timestamp_ns = 0;
        uint64_t restore_sequence = 0;
        bool cancelled = false;
    };
private:
    mutable std::mutex mutex;
    State value;
    bool restoration_submitted = false;
    void identity(const std::string &session, uint64_t round) const {
        if (session != value.session_id || round != value.round_id || !round)
            throw std::runtime_error("wrong phase session or round");
    }
public:
    State snapshot() const { std::lock_guard<std::mutex> lock(mutex); return value; }
    State bind(const std::string &session, uint64_t round) {
        std::lock_guard<std::mutex> lock(mutex);
        if (!round || session.empty()) throw std::runtime_error("invalid phase binding");
        if (value.session_id == session && value.round_id == round) return value;
        if (!value.session_id.empty() && (value.session_id != session || round <= value.round_id))
            throw std::runtime_error("stale or different phase binding");
        if (value.state == "submitting" || value.state == "settling") throw std::runtime_error("phase correction still active");
        value = State {}; value.session_id = session; value.round_id = round;
        restoration_submitted = false;
        return value;
    }
    State query(const std::string &session, uint64_t round, uint64_t id = 0) const {
        std::lock_guard<std::mutex> lock(mutex); identity(session, round);
        if (id && id != value.correction_id) throw std::runtime_error("unknown phase correction");
        return value;
    }
    State adjust(const std::string &session, uint64_t round, uint64_t id, int delta,
                 int count, int64_t expires, int64_t now, int64_t mono,
                 int64_t baseline, int64_t minimum, int64_t maximum) {
        std::lock_guard<std::mutex> lock(mutex); identity(session, round);
        if (id && id == value.correction_id) {
            if (delta == value.delta_us && count == value.count && expires == value.expires_ns) return value;
            throw std::runtime_error("conflicting phase correction duplicate");
        }
        if (!id || id <= value.correction_id || delta < -1000 || delta > 1000 || !delta || count < 1 || count > 16 ||
            minimum <= 0 || maximum < minimum || baseline < minimum || baseline > maximum ||
            (delta < 0 && baseline - minimum < -delta) || (delta > 0 && maximum - baseline < delta) ||
            now <= 0 || expires <= now || expires - now > 5000000000LL || mono < 0 ||
            mono > std::numeric_limits<int64_t>::max() - (expires - now))
            throw std::runtime_error("invalid, expired or out-of-range phase correction");
        if (value.state == "submitting" || value.state == "settling") throw std::runtime_error("overlapping phase correction");
        value.correction_id = id; value.delta_us = delta; value.count = count;
        value.submitted_frames = 0; value.expires_ns = expires; value.deadline_mono_ns = mono + (expires - now);
        value.restored_timestamp_ns = 0; value.restore_sequence = 0; value.cancelled = false;
        value.state = "submitting"; restoration_submitted = false;
        return value;
    }
    State cancel(const std::string &session, uint64_t round) {
        std::lock_guard<std::mutex> lock(mutex); identity(session, round);
        if (value.state == "submitting" || value.state == "settling") value.cancelled = true;
        return value;
    }
    void cancel_active() {
        std::lock_guard<std::mutex> lock(mutex);
        if (value.state == "submitting" || value.state == "settling") value.cancelled = true;
    }
    Submission next(int64_t baseline, int64_t mono) {
        std::lock_guard<std::mutex> lock(mutex);
        if (value.state == "submitting") {
            if (mono >= value.deadline_mono_ns) value.cancelled = true;
            if (!value.cancelled && value.submitted_frames < value.count) {
                ++value.submitted_frames;
                return {baseline + value.delta_us, value.correction_id, false, value.round_id};
            }
            value.state = "settling";
        }
        if (value.state == "settling" && !restoration_submitted) {
            restoration_submitted = true;
            return {baseline, value.correction_id, true, value.round_id};
        }
        return {baseline, 0, false, value.round_id};
    }
    void completed(const Submission &submitted, uint64_t sequence, int64_t timestamp) {
        std::lock_guard<std::mutex> lock(mutex);
        if (submitted.restoration && submitted.correction_id == value.correction_id && submitted.round_id == value.round_id && value.state == "settling") {
            value.restore_sequence = sequence; value.restored_timestamp_ns = timestamp; value.state = "restored";
        }
    }
};
}
