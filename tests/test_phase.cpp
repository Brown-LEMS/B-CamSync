#include "bcamsync/actuator.hpp"
#include "bcamsync/phase.hpp"
#include <cmath>
#include <iostream>
#include <limits>
#include <stdexcept>

using namespace bcamsync;
namespace {
constexpr std::int64_t epoch = 1780000000000000000LL;
void require(bool condition, const char* message) {
    if (!condition)
        throw std::runtime_error(message);
}
template <class F> void rejects(F action) {
    bool rejected = false;
    try {
        action();
    } catch (const std::exception&) {
        rejected = true;
    }
    require(rejected, "expected rejection");
}
TimingSnapshot snapshot(std::int64_t end, std::int64_t phase = 0, std::uint64_t last = 200) {
    TimingSnapshot value;
    value.unit_id = phase ? "follower" : "reference";
    value.snapshot_timestamp_ns = end + 1000000;
    for (std::uint64_t i = 0; i < 121; ++i) {
        FrameTiming f;
        f.camera_sequence = last - 120 + i;
        f.sensor_timestamp_ns =
            100000000000LL + static_cast<std::int64_t>(f.camera_sequence) * 25000000 + phase;
        f.timestamp_ns = end - static_cast<std::int64_t>(120 - i) * 25000000 + phase;
        value.frames.push_back(f);
    }
    value.snapshot_timestamp_ns += phase;
    return value;
}
void estimator() {
    const auto m = estimate_phase(snapshot(epoch), snapshot(epoch, 123457), epoch + 1000000);
    require(m.phase_ns == 123457, "integer epoch difference must retain nanoseconds");
    require(m.absolute_p95_ns == 123457 && m.absolute_max_ns == 123457, "phase statistics");
    require(m.pair_count >= 79 && m.master_period_ns == 25000000, "phase pairing/cadence");
    auto bad = snapshot(epoch);
    bad.frames[30].camera_sequence += 1;
    rejects([&] { estimate_phase(bad, snapshot(epoch), epoch); });
    bad = snapshot(epoch);
    bad.frames[30].mapping_span_ns = 100001;
    rejects([&] { estimate_phase(bad, snapshot(epoch), epoch); });
    rejects([&] { estimate_phase(snapshot(epoch), snapshot(epoch), epoch + 2000000000); });
    rejects(
        [&] { estimate_phase(snapshot(epoch), snapshot(epoch), epoch, {}, epoch - 1000000000); });
    const auto negative =
        estimate_phase(snapshot(epoch), snapshot(epoch, -500000), epoch + 1000000);
    require(negative.phase_ns == -500000, "negative phase correspondence");
    const auto unwrapped = estimate_phase(snapshot(epoch), snapshot(epoch, -12400000),
                                          epoch + 1000000, {}, 0, 12600000);
    require(unwrapped.phase_ns == 12600000, "phase unwrap maintains physical correspondence");
    for (const auto previous : {std::numeric_limits<double>::quiet_NaN(),
                               std::numeric_limits<double>::infinity(),
                               -std::numeric_limits<double>::infinity()}) {
        bool invalid = false;
        try {
            estimate_phase(snapshot(epoch), snapshot(epoch), epoch + 1000000, {}, 0, previous);
        } catch (const std::invalid_argument&) {
            invalid = true;
        }
        require(invalid, "nonfinite phase anchors must be rejected before FFI serialization");
    }
}
void reference_timing() {
    const auto value = snapshot(epoch);
    validate_timing(value, epoch + 1000000);
    rejects([&] { validate_timing(value, epoch + 2000000000); });
    rejects([&] { validate_timing(value, epoch - 100001); });
    rejects([&] { validate_timing(value, std::numeric_limits<std::int64_t>::min()); });
    auto bad = value;
    bad.snapshot_timestamp_ns = std::numeric_limits<std::int64_t>::min();
    rejects([&] { validate_timing(bad, epoch); });
    bad = value;
    bad.frames.back().timestamp_ns = std::numeric_limits<std::int64_t>::min();
    rejects([&] { validate_timing(bad, epoch); });
    bad = value;
    bad.frames[1].sensor_timestamp_ns = std::numeric_limits<std::int64_t>::min();
    rejects([&] { validate_timing(bad, epoch); });
    bad = value;
    bad.frames[1].timestamp_ns = bad.frames[0].timestamp_ns;
    rejects([&] { validate_timing(bad, epoch); });
    bad = value;
    bad.frames[0].camera_sequence = std::numeric_limits<std::uint64_t>::max();
    rejects([&] { validate_timing(bad, epoch); });
    bad = value;
    bad.frames.back().timestamp_ns = std::numeric_limits<std::int64_t>::max();
    bad.snapshot_timestamp_ns = std::numeric_limits<std::int64_t>::max();
    rejects([&] { validate_timing(bad, 1); });
    rejects([&] { validate_timing({}, epoch); });
    bad = value;
    bad.frames[40].sensor_timestamp_ns += 15000000;
    rejects([&] { estimate_phase(bad, value, epoch + 1000000); });
}
void controller() {
    PhaseController c;
    auto d = c.tick(snapshot(epoch), snapshot(epoch, 100000), epoch + 1000000, 1000000000);
    require(!d.ready && d.measurement && !d.action, "first good window cannot grant readiness");
    d = c.tick(snapshot(epoch + 1000000000, 0, 240), snapshot(epoch + 1000000000, 100000, 240),
               epoch + 1001000000, 2000000000);
    require(!d.ready, "overlapping windows cannot grant readiness");
    d = c.tick(snapshot(epoch + 2025000000, 0, 281), snapshot(epoch + 2025000000, 100000, 281),
               epoch + 2026000000, 3025000000);
    require(d.ready && d.state == "tracking", "disjoint good windows grant readiness");
    d = c.tick(snapshot(epoch + 2025000000, 0, 281), snapshot(epoch + 2025000000, 100000, 281),
               epoch + 2026000000, 4126000000);
    require(d.state == "fault" && d.fault.find("stalled") != std::string::npos,
            "monotonic stalled evidence deadline");
    c.reset();
    d = c.tick(snapshot(epoch), snapshot(epoch, 4000000), epoch + 5000000, 0);
    require(d.action && d.action->delta_us < 0 && std::abs(d.action->delta_us) <= 1000 &&
                d.action->count == 16,
            "bounded startup correction");
    const auto id = d.action->correction_id;
    auto follower = snapshot(epoch + 1000000000, 0, 240);
    follower.correction = {"restored", id, epoch + 500000000};
    d = c.tick(snapshot(epoch + 1000000000, 0, 240), follower, epoch + 1001000000, 1000000000);
    require(d.state == "settling" && !d.action, "restoration starts settled-window wait");
    follower = snapshot(epoch + 3000000000, 0, 320);
    follower.correction = {"restored", id, epoch + 500000000};
    d = c.tick(snapshot(epoch + 3000000000, 0, 320), follower, epoch + 3001000000, 3000000000);
    require(d.measurement && d.measurement->drift_ns_per_second == 0,
            "actuation step is not oscillator drift");
    d = c.tick(snapshot(epoch + 3000000000, 0, 320), follower, epoch + 3001000000, 4000000000,
               false);
    require(d.fault == "clock health lost" && !d.ready, "clock loss faults controller");
    c.reset();
    c.tick(snapshot(epoch), snapshot(epoch), epoch + 1000000, 0);
    auto remapped = snapshot(epoch);
    remapped.snapshot_timestamp_ns += 1000;
    for (auto& frame : remapped.frames) frame.timestamp_ns += 1000;
    d = c.tick(remapped, remapped, epoch + 2000000, 100000000);
    require(d.fault.empty(), "clock refinement preserves immutable sensor frame identity");
    remapped.frames.back().sensor_timestamp_ns += 1;
    d = c.tick(remapped, remapped, epoch + 2000000, 200000000);
    require(!d.fault.empty(), "same sequence with changed sensor timestamp is rejected");
    c.reset(0);
    d = c.tick({}, {}, epoch, 31000000000);
    require(d.fault.empty() && !d.ready,
            "startup duration belongs to the session rather than the phase controller");
    c.reset();
    c.tick(snapshot(epoch), snapshot(epoch, 4000000), epoch + 5000000, 0);
    d = c.tick(snapshot(epoch + 6000000000, 0, 440), snapshot(epoch + 6000000000, 4000000, 440),
               epoch + 6005000000, 6000000000);
    require(d.fault == "correction acknowledgement timeout", "unacknowledged correction times out");
    PhasePolicy p;
    p.gain = 2;
    rejects([&] { PhaseController invalid(p); });
}
void actuator() {
    PhaseActuator a;
    a.bind("session", 1);
    auto state =
        a.adjust("session", 1, 1, -200, 2, epoch + 2000000000, epoch, 0, 25000, 24000, 26000);
    require(state.state == "submitting", "accepted command");
    a.adjust("session", 1, 1, -200, 2, epoch + 2000000000, epoch, 0, 25000, 24000, 26000);
    rejects([&] {
        a.adjust("session", 1, 1, -100, 2, epoch + 2000000000, epoch, 0, 25000, 24000, 26000);
    });
    rejects([&] {
        a.adjust("session", 1, 2, -200, 2, epoch + 2000000000, epoch, 0, 25000, 24000, 26000);
    });
    rejects([&] { a.query("other", 1); });
    require(a.next(25000, 0).duration_us == 24800, "first pulse duration");
    require(a.next(25000, 1).duration_us == 24800, "second pulse duration");
    const auto restore = a.next(25000, 2);
    require(restore.restoration && restore.duration_us == 25000,
            "baseline restoration is submitted");
    require(a.snapshot().state == "settling", "submission does not acknowledge restoration");
    auto wrong = restore;
    wrong.round_id = 2;
    a.completed(wrong, 10, epoch);
    require(a.snapshot().state == "settling", "wrong-round completion ignored");
    a.completed(restore, 10, epoch);
    require(a.snapshot().restored_timestamp_ns == epoch && a.snapshot().state == "restored",
            "observed completion acknowledges restoration");
    a.bind("session", 2);
    rejects([&] { a.bind("session", 1); });
    rejects([&] { a.adjust("session", 2, 1, 1001, 1, epoch + 1, epoch, 0, 25000, 24000, 27000); });
    rejects([&] { a.adjust("session", 2, 1, 100, 1, epoch, epoch, 0, 25000, 24000, 27000); });
    rejects([&] {
        a.adjust("session", 2, 1, 100, 1, epoch + 1, epoch, 0,
                 std::numeric_limits<std::int64_t>::max(), 1,
                 std::numeric_limits<std::int64_t>::max());
    });
    a.adjust("session", 2, 1, -200, 16, epoch + 1000000000, epoch, 0, 25000, 24000, 26000);
    require(a.next(25000, 1000000000).restoration, "expiry restores without wall-clock dependency");
    a.completed(a.next(25000, 1000000001), 11, epoch); // Non-restoration cannot acknowledge.
    require(a.snapshot().state == "settling", "unrelated completion ignored");
}
} // namespace
int main() {
    try {
        estimator();
        reference_timing();
        controller();
        actuator();
        std::cout << "phase tests passed\n";
    } catch (const std::exception& e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
