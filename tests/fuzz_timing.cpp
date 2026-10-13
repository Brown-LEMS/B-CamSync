#include "bcamsync/phase.hpp"
#include "wire.hpp"
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <exception>

namespace {
class Input {
  public:
    Input(const std::uint8_t* data, std::size_t size) : data_(data), size_(size) {}
    std::uint64_t word() {
        std::uint64_t value = 0;
        for (unsigned shift = 0; shift < 64 && position_ < size_; shift += 8)
            value |= static_cast<std::uint64_t>(data_[position_++]) << shift;
        return value;
    }
    std::int64_t signed_word() {
        const auto bits = word();
        std::int64_t result;
        std::memcpy(&result, &bits, sizeof(result));
        return result;
    }

  private:
    const std::uint8_t* data_;
    std::size_t size_, position_ = 0;
};
template <class Operation> void checked(Operation operation) {
    try {
        operation();
    } catch (const std::exception&) {
    }
}
void decode(const bcamsync::detail::Json& message) {
    checked([&] {
        const auto snapshot = bcamsync::detail::snapshot_from_json(message);
        bcamsync::validate_timing(snapshot, snapshot.snapshot_timestamp_ns);
        bcamsync::estimate_phase(snapshot, snapshot, snapshot.snapshot_timestamp_ns);
    });
    checked([&] { bcamsync::detail::status_from_json(message); });
}
} // namespace
extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t* data, std::size_t size) {
    if (!size || size > 16384)
        return 0;
    if (data[0] & 1) {
        const auto message = bcamsync::detail::Json::parse(
            reinterpret_cast<const char*>(data + 1), reinterpret_cast<const char*>(data + size),
            nullptr, false);
        if (!message.is_discarded())
            decode(message);
    }
    Input input(data, size);
    constexpr std::int64_t epoch = 1780000000000000000LL, period = 25000000;
    bcamsync::TimingSnapshot reference;
    reference.unit_id = "reference";
    reference.snapshot_timestamp_ns = epoch + 4000000000;
    const auto count = 82 + input.word() % 48;
    for (std::uint64_t i = 0; i < count; ++i) {
        bcamsync::FrameTiming frame;
        frame.camera_sequence = i;
        frame.timestamp_ns =
            reference.snapshot_timestamp_ns - static_cast<std::int64_t>(count - 1 - i) * period;
        frame.sensor_timestamp_ns = 100000000000LL + static_cast<std::int64_t>(i) * period;
        reference.frames.push_back(frame);
    }
    auto follower = reference;
    follower.unit_id = "follower";
    const auto phase = input.signed_word() % period;
    follower.snapshot_timestamp_ns += phase;
    for (auto& frame : follower.frames) {
        frame.timestamp_ns += phase;
        frame.sensor_timestamp_ns += phase;
    }
    const auto mutation = input.word();
    auto& frame = follower.frames[static_cast<std::size_t>(input.word() % count)];
    if (mutation & 1)
        frame.timestamp_ns = input.signed_word();
    if (mutation & 2)
        frame.sensor_timestamp_ns = input.signed_word();
    if (mutation & 4)
        frame.mapping_span_ns = input.signed_word();
    if (mutation & 8)
        frame.camera_sequence = input.word();
    if (mutation & 16)
        follower.snapshot_timestamp_ns = input.signed_word();
    follower.correction.correction_id = input.word();
    follower.correction.restored_timestamp_ns = input.signed_word();
    const auto now = reference.snapshot_timestamp_ns + period;
    checked([&] { bcamsync::validate_timing(follower, now); });
    checked([&] { bcamsync::estimate_phase(reference, follower, now); });
    checked([&] {
        bcamsync::PhaseController controller;
        controller.tick(reference, follower, now, 0);
        controller.tick(reference, follower, now, 2000000000);
    });
    decode(bcamsync::detail::snapshot_json(follower));
    bcamsync::GroupStatus status;
    status.state = "tracking";
    status.ready = (mutation & 32) != 0;
    bcamsync::UnitStatus unit;
    unit.unit_id = follower.unit_id;
    unit.phase_ms = static_cast<double>(phase) / 1e6;
    unit.measurement_end_ns = input.signed_word();
    status.units.push_back(unit);
    decode(bcamsync::detail::status_json(status));
    return 0;
}
