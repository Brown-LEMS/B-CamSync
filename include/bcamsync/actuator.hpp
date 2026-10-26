#pragma once
#include <cstdint>
#include <string>

namespace bcamsync {
/// Adapter support for bounded corrections; the capture path applies each next() result.
/// Request submission and observed completion are separate boundaries.
class PhaseActuator {
  public:
    struct Submission {
        int64_t duration_us = 0;
        uint64_t correction_id = 0;
        bool restoration = false;
        uint64_t round_id = 0;
    };
    struct State {
        std::string session_id, state = "idle";
        uint64_t round_id = 0, correction_id = 0;
        int delta_us = 0, count = 0, submitted_frames = 0;
        int64_t expires_ns = 0, deadline_mono_ns = 0, restored_timestamp_ns = 0;
        uint64_t restore_sequence = 0;
        bool cancelled = false;
    };
    PhaseActuator();
    ~PhaseActuator();
    PhaseActuator(const PhaseActuator&) = delete;
    PhaseActuator& operator=(const PhaseActuator&) = delete;
    State snapshot() const;
    State bind(const std::string& session, uint64_t round);
    State query(const std::string& session, uint64_t round, uint64_t id = 0) const;
    State adjust(const std::string& session, uint64_t round, uint64_t id, int delta, int count,
                 int64_t expires, int64_t now, int64_t mono, int64_t baseline, int64_t minimum,
                 int64_t maximum);
    State cancel(const std::string& session, uint64_t round);
    void cancel_active();
    Submission next(int64_t baseline, int64_t mono);
    void completed(const Submission& submitted, uint64_t sequence, int64_t timestamp);
  private:
    void* handle_;
};
} // namespace bcamsync
