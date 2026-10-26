#include "bcamsync/sync.hpp"
#include "bcamsync/detail/sync_runtime.hpp"
#include <atomic>
#include <mutex>
#include <thread>

namespace bcamsync {
class SyncSession::Impl {
public:
    detail::SyncRuntime runtime;
    std::mutex lifecycle;
    std::thread worker;
    std::atomic<bool> finished{true};
    Impl(SyncConfig config, std::string node, LocalSyncUnits units)
        : runtime(std::move(config), std::move(node), std::move(units)) {}
    void start() {
        std::lock_guard<std::mutex> lock(lifecycle);
        if (worker.joinable() && !finished.load()) {
            const auto state = runtime.status().state;
            if (state != "stopped" && state != "fault")
                throw std::runtime_error("session already started; stop before restarting");
        }
        if (worker.joinable()) worker.join();
        runtime.begin();
        finished = false;
        worker = std::thread([this] { runtime.run(); runtime.finish(); finished = true; });
    }
    void stop() {
        std::lock_guard<std::mutex> lock(lifecycle);
        if (!finished.load()) runtime.request_stop();
        if (worker.joinable()) worker.join();
    }
};
SyncSession::SyncSession(SyncConfig config, std::string node, LocalSyncUnits units)
    : impl_(std::make_unique<Impl>(std::move(config), std::move(node), std::move(units))) {}
SyncSession::~SyncSession() { stop(); }
void SyncSession::start() { impl_->start(); }
void SyncSession::set_node_addresses(const std::map<std::string, std::string>& addresses) {
    std::lock_guard<std::mutex> lock(impl_->lifecycle);
    if (impl_->worker.joinable())
        throw std::runtime_error("stop the session before changing node addresses");
    impl_->runtime.set_node_addresses(addresses);
}
bool SyncSession::wait_ready(std::chrono::milliseconds timeout) { return impl_->runtime.wait_ready(timeout); }
GroupStatus SyncSession::status() const { return impl_->runtime.status(); }
ClockMapping SyncSession::clock() const { return impl_->runtime.clock(); }
void SyncSession::stop() { impl_->stop(); }
} // namespace bcamsync
