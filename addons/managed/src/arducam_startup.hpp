#pragma once

#include <chrono>
#include <cstdint>

namespace bcamsync::detail {
class InterruptibleStartup {
public:
    virtual ~InterruptibleStartup() = default;
    virtual void interrupt_startup() = 0;
};

struct ArducamStartup {
    using Clock = std::chrono::steady_clock;
    using Time = Clock::time_point;
    static constexpr unsigned max_retries = 20;
    enum class Result { waiting, ready, cancelled, no_frames, controls_timeout };

    Time first_frame{}, last_frame{};
    unsigned valid_frames = 0, stable_frames = 0;
    std::uint32_t last_sequence = 0;
    bool cancelled = false;

    void invalid_frame() { stable_frames = 0; }

    void observe(Time now, std::uint32_t sequence, bool controls_settled) {
        if (!valid_frames) first_frame = now;
        if (valid_frames && sequence != static_cast<std::uint32_t>(last_sequence + 1u)) invalid_frame();
        last_sequence = sequence;
        last_frame = now;
        ++valid_frames;
        stable_frames = controls_settled ? stable_frames + 1 : 0;
    }

    Result result(Time now, Time controls_deadline, Clock::duration max_gap, unsigned required_frames) const {
        if (cancelled) return Result::cancelled;
        if (now >= last_frame + max_gap) return Result::no_frames;
        if (stable_frames >= required_frames) return Result::ready;
        if (now >= controls_deadline) return Result::controls_timeout;
        return Result::waiting;
    }
};
}
