#include "bcamsync/session.hpp"
#include "python_frame.hpp"
#include <pybind11/pybind11.h>
#include <arpa/inet.h>
#include <atomic>
#include <stdexcept>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>

namespace py = pybind11;
using namespace bcamsync;

namespace {
std::shared_ptr<const FramePacket> packet() {
    auto frame = std::make_shared<FramePacket>();
    frame->unit_id = "fixture-board";
    frame->sequence = 17;
    frame->width = 8;
    frame->height = 3;
    frame->views = 4;
    frame->sensor_timestamp_ns = frame->timestamp_ns = monotonic_ns();
    auto storage = std::make_shared<FrameStorage>();
    storage->pixels.resize(24);
    for (unsigned i = 0; i < 24; ++i) storage->pixels[i] = static_cast<std::uint8_t>(i);
    frame->storage = std::move(storage);
    return frame;
}

std::uint16_t free_port() {
    const auto fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) throw std::runtime_error("Cannot open test endpoint");
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(fd, reinterpret_cast<sockaddr *>(&address), sizeof(address)) != 0) {
        close(fd);
        throw std::runtime_error("Cannot bind test endpoint");
    }
    socklen_t size = sizeof(address);
    const auto result = getsockname(fd, reinterpret_cast<sockaddr *>(&address), &size);
    close(fd);
    if (result != 0) throw std::runtime_error("Cannot discover test endpoint");
    return ntohs(address.sin_port);
}

class FixtureBackend final : public AcquisitionBackend {
public:
    explicit FixtureBackend(bool empty_reads) : empty_reads_(empty_reads) {}
    void start(const std::string &, std::uint64_t, bool) override { running_ = true; }
    void stop() override { running_ = false; }
    TimingSnapshot snapshot() const override {
        TimingSnapshot result;
        result.unit_id = "fixture-board";
        result.snapshot_timestamp_ns = monotonic_ns();
        constexpr std::int64_t period = 25000000;
        const auto last = static_cast<std::uint64_t>(result.snapshot_timestamp_ns / period);
        for (std::uint64_t i = 0; i < 240; ++i) {
            FrameTiming timing;
            timing.camera_sequence = last - 239 + i;
            timing.timestamp_ns = timing.sensor_timestamp_ns = static_cast<std::int64_t>(timing.camera_sequence) * period;
            timing.requested_duration_us = timing.reported_duration_us = period / 1000;
            result.frames.push_back(timing);
        }
        return result;
    }
    CorrectionStatus adjust(const Pulse &) override {
        throw std::logic_error("Single-unit Python fixture does not require corrections");
    }
    void cancel() override {}
    std::shared_ptr<const FramePacket> read(std::chrono::milliseconds duration) override {
        if (empty_reads_) {
            std::this_thread::sleep_for(duration);
            return {};
        }
        return running_ ? packet() : nullptr;
    }
    std::string fault() const override { return {}; }
private:
    std::atomic<bool> running_{false};
    bool empty_reads_;
};

std::unique_ptr<Session> make_session(bool empty_reads) {
    Config config;
    config.group_id = "python-ownership-fixture";
    config.reference_unit = "fixture-board";
    config.nodes = {{"fixture-node", "127.0.0.1", "", free_port()}};
    UnitConfig unit;
    unit.id = "fixture-board";
    unit.node_id = "fixture-node";
    unit.camera_id = "software-fixture";
    unit.width = 8;
    unit.height = 3;
    config.units = {unit};
    std::vector<std::unique_ptr<AcquisitionBackend>> backends;
    backends.push_back(std::make_unique<FixtureBackend>(empty_reads));
    return std::make_unique<Session>(std::move(config), "fixture-node", std::move(backends));
}
}

// This extension is built only for software checks and is never installed.
PYBIND11_MODULE(_test_support, module) {
    py::module_::import("bcamsync._native");
    module.def("make_packet", [] { return py::cast(bcamsync::python::FrameHandle{packet()}); });
    module.def("make_session", &make_session, py::arg("empty_reads") = false);
}
