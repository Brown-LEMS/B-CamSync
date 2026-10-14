#include "bcamsync/sync.hpp"
#include <stdexcept>

using namespace bcamsync;
namespace {
class ExistingCapture final : public SyncUnit {
public:
    bool attached = false;
    const std::int64_t frame_ns = boot_time_ns() - 1;
    void bind(const std::string&, std::uint64_t) override { attached = true; }
    TimingSnapshot snapshot() const override {
        TimingSnapshot snapshot;
        snapshot.unit_id = "sensor";
        snapshot.snapshot_timestamp_ns = boot_time_ns();
        FrameTiming frame;
        frame.camera_sequence = 1;
        frame.timestamp_ns = frame.sensor_timestamp_ns = frame_ns;
        snapshot.frames.push_back(frame);
        return snapshot;
    }
    CorrectionStatus adjust(const Pulse&) override {
        throw std::logic_error("reference unit must not receive a correction");
    }
    void cancel() override { attached = false; }
    std::string fault() const override { return {}; }
};
}
// This executable links only synchronization sources, without types.cpp, acquisition, or SDKs.
int main() {
    SyncConfig config;
    config.group_id = "independent-runtime";
    config.reference_unit = "sensor";
    config.nodes = {{"local", "", "", 58080}};
    config.units = {{"sensor", "local"}};
    auto capture = std::make_shared<ExistingCapture>();
    SyncSession synchronization(config, "local", {{"sensor", capture}});
    synchronization.start();
    if (!synchronization.wait_ready(std::chrono::seconds(1))) return 1;
    if (!capture->attached || !synchronization.clock().valid) return 1;
    synchronization.stop();
    return capture->attached || !synchronization.status().fault.empty() ? 1 : 0;
}
