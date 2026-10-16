#include "bcamsync/detail/sync_runtime.hpp"
#include "platform.hpp"
#include "runtime_ffi.hpp"
#include "wire.hpp"
#include <atomic>
#include <cstring>
#include <iomanip>
#include <random>
#include <sstream>

namespace bcamsync::detail {
namespace {
std::string new_identity() {
    std::random_device random;
    std::ostringstream result;
    for (int i = 0; i < 4; ++i)
        result << std::hex << std::setw(8) << std::setfill('0') << random();
    return result.str();
}
Json response(char* output) {
    if (!output) throw std::runtime_error("Rust runtime returned no response");
    const std::unique_ptr<char, decltype(&bcamsync_runtime_release)> owned(output, bcamsync_runtime_release);
    auto value = Json::parse(owned.get());
    if (!value.at("ok").get<bool>()) throw std::runtime_error(value.at("error").get<std::string>());
    return value.at("result");
}
} // namespace

class SyncRuntime::Impl {
public:
    SyncConfig config;
    std::string local, identity = new_identity();
    std::uint64_t round = 0;
    LocalSyncUnits ports;
    NodePlatform platform;
    std::atomic<bool> stopped{true};
    BcamSyncRuntime* runtime = nullptr;

    Impl(SyncConfig c, std::string node, LocalSyncUnits units)
        : config(std::move(c)), local(std::move(node)), ports(std::move(units)), platform(config, local) {
        config.validate();
        config.node(local);
        std::size_t expected = 0;
        for (const auto& unit : config.units) {
            if (unit.node_id != local) continue;
            ++expected;
            if (!ports.count(unit.id) || !ports.at(unit.id))
                throw std::invalid_argument("missing local sync interface: " + unit.id);
        }
        if (ports.size() != expected)
            throw std::invalid_argument("sync interfaces do not match local topology");
        Json unit_ids = Json::array();
        for (const auto& entry : ports) {
            if (!entry.second) throw std::invalid_argument("missing local sync interface: " + entry.first);
            unit_ids.push_back(entry.first);
        }
        const auto input = Json({{"config", Json::parse(config.to_json())}, {"local", local},
                                 {"identity", identity}, {"units", unit_ids}}).dump();
        char* error = nullptr;
        runtime = bcamsync_runtime_create(input.c_str(), this, host_call, host_release, &error);
        if (!runtime) response(error);
    }
    ~Impl() { bcamsync_runtime_destroy(runtime); }
    Json call(const Json& request) const {
        const auto input = request.dump();
        return response(bcamsync_runtime_call(runtime, input.c_str()));
    }
    SyncUnit& port(const std::string& id) {
        const auto found = ports.find(id);
        if (found == ports.end()) throw std::invalid_argument("unit is not local: " + id);
        return *found->second;
    }
    Json dispatch(const Json& input) {
        const std::string op = input.at("op");
        if (op == "platform_start") {
            auto resolved = config;
            platform.start(resolved, stopped, input.at("deadline").get<std::int64_t>());
            return Json::parse(resolved.to_json());
        }
        if (op == "platform_sample") {
            const auto sampled = platform.sample();
            return {{"valid", sampled.valid}, {"offset_ns", sampled.offset_ns},
                    {"uncertainty_ns", sampled.uncertainty_ns}, {"fault", sampled.fault}};
        }
        if (op == "platform_stop") { platform.stop(); return nullptr; }
        auto& unit = port(input.at("unit").get<std::string>());
        if (op == "bind") {
            unit.bind(input.at("identity"), input.at("round"));
            return nullptr;
        }
        if (op == "fault") return unit.fault();
        if (op == "snapshot") return snapshot_json(unit.snapshot());
        if (op == "cancel") { unit.cancel(); return nullptr; }
        if (op == "adjust") {
            const auto& value = input.at("pulse");
            Pulse pulse{value.at("correction_id"), value.at("delta_us"), value.at("count"), value.at("expires_ns")};
            const auto result = unit.adjust(pulse);
            return {{"state", result.state}, {"id", result.correction_id}, {"restored", result.restored_timestamp_ns}};
        }
        throw std::runtime_error("unknown synchronization host operation");
    }
    static char* host_call(void* context, const char* request) noexcept {
        try {
            Json result;
            try { result = {{"ok", true}, {"result", static_cast<Impl*>(context)->dispatch(Json::parse(request))}}; }
            catch (const std::exception& error) { result = {{"ok", false}, {"error", error.what()}}; }
            catch (...) { result = {{"ok", false}, {"error", "unknown synchronization host failure"}}; }
            const auto data = result.dump();
            auto* output = new char[data.size() + 1];
            std::memcpy(output, data.c_str(), data.size() + 1);
            return output;
        } catch (...) {
            // The error response itself may fail to allocate; Rust treats null as a host error.
            return nullptr;
        }
    }
    static void host_release(void*, char* response) noexcept { delete[] response; }
};
SyncRuntime::SyncRuntime(SyncConfig config, std::string node, LocalSyncUnits units)
    : impl_(std::make_unique<Impl>(std::move(config), std::move(node), std::move(units))) {}
SyncRuntime::~SyncRuntime() = default;
void SyncRuntime::begin() {
    impl_->stopped = false;
    const auto result = impl_->call({{"op", "begin"}});
    impl_->round = result.at("round");
}
void SyncRuntime::set_node_addresses(const std::map<std::string, std::string>& addresses) {
    try { impl_->call({{"op", "set_node_addresses"}, {"addresses", addresses}}); }
    catch (const std::runtime_error& error) { throw std::invalid_argument(error.what()); }
    for (auto& node : impl_->config.nodes) node.address = addresses.at(node.id);
    impl_->config.network_mode = "existing";
}
void SyncRuntime::run() { impl_->call({{"op", "run"}}); }
void SyncRuntime::request_stop() { impl_->stopped = true; impl_->call({{"op", "request_stop"}}); }
bool SyncRuntime::stopping() const { return impl_->call({{"op", "stopping"}}).get<bool>(); }
void SyncRuntime::wait_for_stop() { impl_->call({{"op", "wait_for_stop"}}); }
void SyncRuntime::finish() { impl_->call({{"op", "finish"}}); }
void SyncRuntime::fail(const std::string& reason) { impl_->call({{"op", "fail"}, {"reason", reason}}); }
const std::string& SyncRuntime::identity() const { return impl_->identity; }
std::uint64_t SyncRuntime::round() const { return impl_->round; }
GroupStatus SyncRuntime::status() const { return status_from_json(impl_->call({{"op", "status"}})); }
ClockMapping SyncRuntime::clock() const {
    const auto result = impl_->call({{"op", "clock"}});
    return {result.at("valid"), result.at("offset_ns"), result.at("uncertainty_ns")};
}
bool SyncRuntime::wait_ready(std::chrono::milliseconds timeout) {
    return impl_->call({{"op", "wait_ready"}, {"milliseconds", timeout.count()}}).get<bool>();
}
} // namespace bcamsync::detail
