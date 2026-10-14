#include "bcamsync/fake.hpp"
#include "bcamsync/phase.hpp"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <ctime>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <thread>

using namespace bcamsync;
using namespace std::chrono_literals;
namespace {
void require(bool condition, const char* message) {
    if (!condition)
        throw std::runtime_error(message);
}
std::int64_t boot_ns() {
    timespec value{};
    if (clock_gettime(CLOCK_BOOTTIME, &value) != 0)
        throw std::runtime_error("cannot read test capture clock");
    return static_cast<std::int64_t>(value.tv_sec) * 1000000000LL + value.tv_nsec;
}
template <class Action> void rejects(Action action) {
    bool rejected = false;
    try {
        action();
    } catch (const std::exception&) {
        rejected = true;
    }
    require(rejected, "expected fake backend rejection");
}
template <class Predicate>
void await(Predicate predicate, std::chrono::milliseconds timeout, const char* message) {
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    while (!predicate()) {
        if (std::chrono::steady_clock::now() >= deadline)
            throw std::runtime_error(message);
        std::this_thread::sleep_for(5ms);
    }
}
UnitConfig unit(const std::string& id) {
    UnitConfig config;
    config.id = id;
    config.node_id = "node";
    config.camera_id = id;
    config.fake.width = 32;
    config.fake.height = 8;
    config.queue_capacity = 2;
    config.pool_capacity = 4;
    return config;
}
Pulse pulse(AcquisitionBackend& backend, std::uint64_t id, int delta, int count) {
    return {id, delta, count, backend.snapshot().snapshot_timestamp_ns + 5000000000LL};
}
void phase_and_packets() {
    std::cout << "checking autonomous fake capture, phase controls, and packet ownership\n"
              << std::flush;
    auto a = unit("reference"), b = unit("follower");
    b.fake.initial_phase_us = 4000;
    auto cameras = make_fake_backends({a, b});
    auto &reference = *cameras[0], &follower = *cameras[1];
    reference.start("fake-session", 1);
    follower.start("fake-session", 1);
    await(
        [&] {
            return reference.snapshot().frames.size() >= 94 &&
                   follower.snapshot().frames.size() >= 94;
        },
        4s, "fake timing must progress without application image reads");
    const auto left = reference.snapshot(), right = follower.snapshot();
    for (const auto* snapshot : {&left, &right})
        for (const auto& frame : snapshot->frames)
            require(frame.timestamp_ns == frame.sensor_timestamp_ns && frame.mapping_span_ns == 0,
                    "backend timing retains sensor timestamps without common-clock mapping");
    const auto initial = estimate_phase(
        left, right, std::max(left.snapshot_timestamp_ns, right.snapshot_timestamp_ns));
    require(std::abs(initial.phase_ns - 4000000) <= 1, "configured initial phase is measurable");
    auto held = reference.read(500ms);
    require(held && held->views == 4 && held->view_width() == 8 && held->dropped_before > 0,
            "slow consumption drops old packets and retains four camera views");
    require(held->timestamp_ns == held->sensor_timestamp_ns,
            "backend image packets retain node-local sensor time");
    const auto pixel = held->view_data(0)[0];
    require(pixel == static_cast<std::uint8_t>(held->sequence),
            "synthetic pixel identity follows capture sequence");
    require(held->view_data(2)[32] == static_cast<std::uint8_t>(held->sequence + 48),
            "camera views retain board stride");
    std::vector<std::shared_ptr<const FramePacket>> retained{held};
    while (retained.size() < a.pool_capacity) {
        const auto frame = reference.read(1s);
        require(static_cast<bool>(frame), "fake pool should provide each storage lease");
        retained.push_back(frame);
    }
    const auto before = reference.snapshot().frames.back().camera_sequence;
    require(!reference.read(100ms),
            "fully retained storage drops images without reusing user memory");
    const auto after = reference.snapshot().frames.back().camera_sequence;
    require(after > before, "timing continues while the image pool is exhausted");
    require(held->view_data(0)[0] == pixel, "leased packet contents stay immutable");
    retained.clear();
    require(static_cast<bool>(reference.read(1s)), "releasing storage resumes image delivery");
    const auto command = pulse(follower, 1, -200, 4);
    require(follower.adjust(command).correction_id == 1 &&
                follower.adjust(command).correction_id == 1,
            "fake backend preserves command identity and duplicate semantics");
    await([&] { return follower.snapshot().correction.state == "restored"; }, 2s,
          "fake correction must restore baseline");
    const auto restored = follower.snapshot();
    require(restored.correction.restored_timestamp_ns > 0,
            "restoration includes actual capture boundary");
    std::size_t corrected_frames = 0;
    for (const auto& frame : restored.frames)
        if (frame.correction_id == 1 && frame.requested_duration_us == 24800)
            ++corrected_frames;
    require(corrected_frames == 4, "bounded pulse changes exactly its requested frame intervals");
    await(
        [&] {
            return follower.snapshot().frames.back().timestamp_ns >=
                   restored.correction.restored_timestamp_ns + 2300000000LL;
        },
        4s, "post-restoration timing window must fill");
    const auto settled_left = reference.snapshot(), settled_right = follower.snapshot();
    const auto settled = estimate_phase(
        settled_left, settled_right,
        std::max(settled_left.snapshot_timestamp_ns, settled_right.snapshot_timestamp_ns), {},
        restored.correction.restored_timestamp_ns + 250000000);
    require(std::abs(settled.phase_ns - 3200000) <= 1,
            "pulse duration changes physical phase by its accumulated interval");
    follower.adjust(pulse(follower, 2, 800, 16));
    await(
        [&] {
            const auto snapshot = follower.snapshot();
            for (const auto& frame : snapshot.frames)
                if (frame.correction_id == 2 && frame.requested_duration_us == 25800)
                    return true;
            return false;
        },
        2s, "active correction should reach capture before shutdown");
    follower.stop();
    follower.stop();
    const auto stopped = follower.snapshot();
    require(stopped.correction.state == "restored" &&
                stopped.frames.back().requested_duration_us == 25000,
            "stop observes baseline restoration before terminating fake capture");
    require(follower.fault().empty(), "successful restoration has no shutdown fault");
    rejects([&] { follower.adjust(command); });
    require(!follower.read(10ms), "closed fake camera ends image delivery");
    follower.start("new-fake-session", 1);
    require(static_cast<bool>(follower.read(1s)),
            "fake camera restarts with a new synchronization identity");
    require(follower.snapshot().frames.front().camera_sequence == 0,
            "restart begins a new capture sequence");
    follower.stop();
    reference.stop();
    require(held->view_data(0)[0] == pixel &&
                held->view_data(3)[32] == static_cast<std::uint8_t>(held->sequence + 56),
            "held frame storage survives backend shutdown and restart");
}
void drift_and_validation() {
    std::cout << "checking explicit fake timing options\n" << std::flush;
    const auto config = unit("drifting");
    auto cameras = make_fake_backends({config}, {{32, 8, 0, 80}});
    cameras[0]->start("drift-session", 1);
    await([&] { return cameras[0]->snapshot().frames.size() >= 8; }, 2s,
          "drifting sensor should produce frames");
    const auto before = boot_ns();
    const auto snapshot = cameras[0]->snapshot();
    const auto after = boot_ns();
    const auto &last = snapshot.frames.back(),
               &previous = snapshot.frames[snapshot.frames.size() - 2];
    require(last.sensor_timestamp_ns - previous.sensor_timestamp_ns == 25002000,
            "configured frequency drift affects sensor intervals deterministically");
    require(snapshot.snapshot_timestamp_ns >= before && snapshot.snapshot_timestamp_ns <= after,
            "fake observations always use node-local CLOCK_BOOTTIME");
    require(last.timestamp_ns == last.sensor_timestamp_ns && last.timestamp_ns <= after,
            "fake sensor timestamps stay in the observation clock domain");
    cameras[0]->stop();
    rejects([&] { make_fake_backends({config}, std::vector<FakeOptions>{}); });
    rejects([&] { make_fake_backends({config}, {{32, 8, 0, 1001}}); });
    rejects([&] { make_fake_backends({config}, {{32, 8, std::numeric_limits<std::int64_t>::min(), 0}}); });
    auto bad = config;
    bad.fake.width = 0;
    rejects([&] { make_fake_backends({bad}); });
    rejects([&] { cameras[0]->start("", 0); });
    auto fast = make_fake_backends({config}, 80);
    fast[0]->start("group-cadence", 1);
    await([&] { return fast[0]->snapshot().frames.size() >= 3; }, 1s, "group FPS drives fake cadence");
    const auto timing = fast[0]->snapshot();
    require(timing.frames.back().requested_duration_us == 12500,
            "group cadence replaces per-unit FPS and has no 45 FPS policy ceiling");
    fast[0]->stop();
    auto buffered = config;
    buffered.queue_capacity = 65;
    buffered.pool_capacity = 66;
    auto ample = make_fake_backends({buffered});
    ample[0]->start("larger-queue", 1);
    require(static_cast<bool>(ample[0]->read(1s)), "storage policy accepts any representable pool larger than queue");
    ample[0]->stop();
    rejects([&] { make_fake_backends({config}, 0); });
    buffered.pool_capacity = buffered.queue_capacity;
    rejects([&] { make_fake_backends({buffered}); });
}
} // namespace
int main() {
    try {
        phase_and_packets();
        drift_and_validation();
        std::cout << "fake backend tests passed\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
