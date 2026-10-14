#include "bcamsync/session.hpp"
#include "bcamsync/fake.hpp"
#include "sync_runtime.hpp"
#include <algorithm>
#include <mutex>
#include <thread>

namespace bcamsync {
namespace {
std::vector<UnitConfig> local_units(const Config& config, const std::string& node) {
    std::vector<UnitConfig> result;
    for (const auto& unit : config.units)
        if (unit.node_id == node) result.push_back(unit);
    return result;
}
std::vector<std::unique_ptr<AcquisitionBackend>> validated_backends(const Config& config,
                                                                 const std::string& node) {
    config.validate();
    config.node(node);
    const auto units = local_units(config, node);
    return config.backend == "fake" ? make_fake_backends(units, config.fps)
                                    : make_arducam_backends(units, config.fps);
}
/// Managed acquisition binds controls while initializing its backend; synchronization observes it.
class ManagedUnit final : public SyncUnit {
public:
    explicit ManagedUnit(AcquisitionBackend& backend) : backend_(backend) {}
    void start_capture(const std::string& identity, std::uint64_t round) {
        backend_.start(identity, round);
        identity_ = identity;
        round_ = round;
    }
    void bind(const std::string& identity, std::uint64_t round) override {
        if (identity != identity_ || round != round_)
            throw std::logic_error("managed capture control binding differs");
    }
    TimingSnapshot snapshot() const override { return backend_.snapshot(); }
    CorrectionStatus adjust(const Pulse& pulse) override { return backend_.adjust(pulse); }
    void cancel() override { backend_.cancel(); }
    std::string fault() const override { return backend_.fault(); }
private:
    AcquisitionBackend& backend_;
    std::string identity_;
    std::uint64_t round_ = 0;
};
} // namespace

class Session::Impl {
public:
    std::vector<std::unique_ptr<AcquisitionBackend>> backends;
    std::vector<UnitConfig> units;
    std::vector<std::shared_ptr<ManagedUnit>> adapters;
    std::unique_ptr<detail::SyncRuntime> sync;
    std::mutex lifecycle;
    std::thread worker;

    Impl(Config config, std::string node, std::vector<std::unique_ptr<AcquisitionBackend>> cameras)
        : backends(std::move(cameras)), units(local_units(config, node)) {
        config.validate();
        config.node(node);
        if (backends.size() != units.size() ||
            std::any_of(backends.begin(), backends.end(), [](const auto& camera) { return !camera; }))
            throw std::invalid_argument("backend count/ownership does not match local topology");
        LocalSyncUnits ports;
        for (std::size_t i = 0; i < units.size(); ++i) {
            auto adapter = std::make_shared<ManagedUnit>(*backends[i]);
            ports.emplace(units[i].id, adapter);
            adapters.push_back(std::move(adapter));
        }
        sync = std::make_unique<detail::SyncRuntime>(config.sync_config(), std::move(node), std::move(ports));
    }
    AcquisitionBackend& backend(const std::string& id) {
        for (std::size_t i = 0; i < units.size(); ++i)
            if (units[i].id == id) return *backends[i];
        throw std::invalid_argument("unit is not local: " + id);
    }
    void cleanup_capture() {
        for (std::size_t i = 0; i < backends.size(); ++i) {
            try { backends[i]->cancel(); }
            catch (const std::exception& error) { sync->fail(units[i].id + ": " + error.what()); }
            try {
                backends[i]->stop();
                const auto fault = backends[i]->fault();
                if (!fault.empty()) sync->fail(units[i].id + ": " + fault);
            } catch (const std::exception& error) { sync->fail(units[i].id + ": " + error.what()); }
        }
    }
    void start() {
        std::lock_guard<std::mutex> lock(lifecycle);
        if (worker.joinable()) throw std::runtime_error("session already started; stop before restarting");
        sync->begin();
        worker = std::thread([this] {
            try {
                for (const auto& adapter : adapters) {
                    if (sync->stopping()) break;
                    adapter->start_capture(sync->identity(), sync->round());
                }
                sync->run();
            } catch (const std::exception& error) {
                if (!sync->stopping()) sync->fail(error.what());
                sync->wait_for_stop();
            } catch (...) {
                if (!sync->stopping()) sync->fail("unknown acquisition failure");
                sync->wait_for_stop();
            }
            cleanup_capture();
            sync->finish();
        });
    }
    void stop() {
        std::lock_guard<std::mutex> lock(lifecycle);
        sync->request_stop();
        if (worker.joinable()) worker.join();
    }
};
Session::Session(Config config, std::string node)
    : Session(config, node, validated_backends(config, node)) {}
Session::Session(Config config, std::string node, std::vector<std::unique_ptr<AcquisitionBackend>> backends)
    : impl_(std::make_unique<Impl>(std::move(config), std::move(node), std::move(backends))) {}
Session::~Session() { stop(); }
void Session::start() { impl_->start(); }
void Session::set_node_addresses(const std::map<std::string, std::string>& addresses) {
    std::lock_guard<std::mutex> lock(impl_->lifecycle);
    if (impl_->worker.joinable())
        throw std::runtime_error("stop the session before changing node addresses");
    impl_->sync->set_node_addresses(addresses);
}
bool Session::wait_ready(std::chrono::milliseconds timeout) { return impl_->sync->wait_ready(timeout); }
GroupStatus Session::status() const { return impl_->sync->status(); }
void Session::stop() { impl_->stop(); }
std::shared_ptr<const FramePacket> Session::read(const std::string& id, std::chrono::milliseconds timeout) {
    auto& acquisition = impl_->backend(id);
    auto packet = acquisition.read(timeout);
    const auto fault = acquisition.fault();
    if (!fault.empty()) throw std::runtime_error(id + ": " + fault);
    if (!packet) return {};
    auto delivered = std::make_shared<FramePacket>(*packet);
    const auto clock = impl_->sync->clock();
    delivered->clock_valid = clock.valid;
    delivered->timestamp_ns = clock.map(packet->sensor_timestamp_ns);
    return delivered;
}
} // namespace bcamsync
