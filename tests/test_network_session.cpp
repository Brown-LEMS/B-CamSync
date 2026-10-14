#include "bcamsync/network.hpp"
#include <atomic>
#include <chrono>
#include <iostream>
#include <stdexcept>
#include <thread>
#include <unistd.h>

// Exercise production discovery on an existing loopback link without activating NetworkManager.
namespace bcamsync::detail {
class NetworkSessionTestAccess {
public:
    static void start_on_link(NetworkSession& session, const std::string& address) {
        session.start_discovery(address);
    }
    static void start(NetworkSession& session, const std::atomic<bool>& stopping, std::int64_t deadline) {
        session.start(stopping, deadline);
    }
};
}
using namespace bcamsync;
using namespace std::chrono_literals;
namespace {
void require(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}
template<class Function> void rejected(Function function, const char* message) {
    bool failed = false;
    try { function(); } catch (const std::exception&) { failed = true; }
    require(failed, message);
}
SyncConfig configuration() {
    SyncConfig config;
    config.group_id = "network-session-" + std::to_string(getpid());
    config.reference_unit = "board-a";
    config.network_mode = "existing";
    config.nodes = {{"node-a", "127.0.0.1", "lo", 58080}, {"node-b", "127.0.0.2", "lo", 58080}};
    config.units = {{"board-a", "node-a"}, {"board-b", "node-b"}};
    return config;
}
void existing_lifetime() {
    const auto config = configuration();
    NetworkSession network(config, "node-a");
    require(network.address().empty(), "unstarted network has no address");
    network.request_stop();
    network.request_stop();
    rejected([&] { network.resolve(1s); }, "resolve must require a started session");
    network.start(1s);
    require(network.address() == "127.0.0.1", "existing network retains its local IPv4 address");
    require(network.resolve(1s).to_json() == config.to_json(), "existing mode preserves explicit peer endpoints");
    rejected([&] { network.start(1s); }, "double start must not replace a live network");
    network.stop();
    network.stop();
    require(network.address().empty(), "stop clears network ownership");
    network.start(1s);
    require(network.resolve(1s).sync_json() == config.sync_json(), "network restart preserves the synchronization contract");
}
void startup_validation() {
    auto config = configuration();
    rejected([&] { NetworkSession network(config, "missing"); }, "network requires a configured local node");
    config.nodes[0].address = "239.1.2.3";
    rejected([&] { NetworkSession network(config, "node-a"); }, "existing network needs a local unicast address");
    config = configuration();
    config.network_mode = "auto";
    config.nodes[0].interface.clear();
    rejected([&] { NetworkSession network(config, "node-a"); }, "auto network requires an interface");
    config.nodes[0].interface = "lo";
    NetworkSession network(config, "node-a");
    rejected([&] { network.start(0ms); }, "zero timeout must not activate a link");
    rejected([&] { network.start(std::chrono::milliseconds::max()); }, "overflowing timeout must not activate a link");
    std::atomic<bool> cancelled{true};
    rejected([&] { detail::NetworkSessionTestAccess::start(network, cancelled, monotonic_ns() + 1000000000LL); },
             "cancelled startup must not contact NetworkManager");
    cancelled = false;
    rejected([&] { detail::NetworkSessionTestAccess::start(network, cancelled, monotonic_ns() - 1); },
             "expired startup must not contact NetworkManager");
    require(network.address().empty(), "rejected startup leaves no network ownership");
}
#ifdef __linux__
void discovery_lifetime() {
    auto config = configuration();
    config.network_mode = "auto";
    NetworkSession first(config, "node-a");
    detail::NetworkSessionTestAccess::start_on_link(first, "127.0.0.1");
    require(first.address() == "127.0.0.1", "local network starts before its peers exist");
    rejected([&] { first.resolve(50ms); }, "missing peer resolution must respect its deadline");
    {
        NetworkSession second(config, "node-b");
        detail::NetworkSessionTestAccess::start_on_link(second, "127.0.0.2");
        const auto resolved = first.resolve(2s);
        require(resolved.network_mode == "existing" && resolved.node("node-b").address == "127.0.0.2",
                "discovery returns the observed peer address for normal synchronization");
        require(second.resolve(2s).node("node-a").address == "127.0.0.1", "both nodes discover independently");
        require(config.network_mode == "auto", "resolution must not mutate the caller's configuration");
    }
    std::this_thread::sleep_for(3200ms);
    rejected([&] { first.resolve(50ms); }, "persistent network must not resolve a departed peer from stale announcements");
    NetworkSession rejoined(config, "node-b");
    detail::NetworkSessionTestAccess::start_on_link(rejoined, "127.0.0.3");
    require(first.resolve(2s).node("node-b").address == "127.0.0.3", "rejoined peers may acquire a new IP address");
}
void contract_check() {
    auto config = configuration();
    config.network_mode = "auto";
    config.group_id += "-contract";
    auto different = config;
    different.fps = 41;
    NetworkSession first(config, "node-a"), second(different, "node-b");
    detail::NetworkSessionTestAccess::start_on_link(first, "127.0.0.1");
    detail::NetworkSessionTestAccess::start_on_link(second, "127.0.0.2");
    bool mismatch = false;
    try { first.resolve(2s); }
    catch (const std::runtime_error& error) {
        mismatch = std::string(error.what()).find("configuration differs") != std::string::npos;
    }
    require(mismatch, "discovery must reject incompatible synchronization contracts");
}
void duplicate_identity_check() {
    auto config = configuration();
    config.network_mode = "auto";
    config.group_id += "-duplicate";
    NetworkSession first(config, "node-a"), second(config, "node-b"), duplicate(config, "node-b");
    detail::NetworkSessionTestAccess::start_on_link(first, "127.0.0.1");
    detail::NetworkSessionTestAccess::start_on_link(second, "127.0.0.2");
    first.resolve(2s);
    detail::NetworkSessionTestAccess::start_on_link(duplicate, "127.0.0.3");
    bool conflict = false;
    const auto deadline = monotonic_ns() + 2000000000LL;
    while (!conflict && monotonic_ns() < deadline) {
        try { first.resolve(100ms); }
        catch (const std::runtime_error& error) {
            conflict = std::string(error.what()).find("multiple addresses claim") != std::string::npos;
        }
        std::this_thread::sleep_for(20ms);
    }
    require(conflict, "two live addresses must not claim the same synchronization node");
}
void stop_cancels_resolution() {
    auto config = configuration();
    config.network_mode = "auto";
    config.group_id += "-cancel";
    NetworkSession network(config, "node-a");
    detail::NetworkSessionTestAccess::start_on_link(network, "127.0.0.1");
    std::atomic<bool> entered{false}, cancelled{false};
    std::thread worker([&] {
        entered = true;
        try { network.resolve(5s); } catch (const std::runtime_error&) { cancelled = true; }
    });
    while (!entered) std::this_thread::yield();
    std::this_thread::sleep_for(20ms);
    const auto before = monotonic_ns();
    const auto local = network.address();
    network.stop();
    worker.join();
    require(local == "127.0.0.1", "local address remains available during peer resolution");
    require(cancelled && monotonic_ns() - before < 1000000000LL, "stop must promptly cancel a waiting resolution");
    require(network.address().empty(), "cancelled network releases its local address");
}
void cancellation_retains_link() {
    auto config = configuration();
    config.network_mode = "auto";
    config.group_id += "-retain";
    NetworkSession network(config, "node-a");
    detail::NetworkSessionTestAccess::start_on_link(network, "127.0.0.1");
    std::atomic<bool> entered{false}, cancelled{false};
    std::thread worker([&] {
        entered = true;
        try { network.resolve(5s); } catch (const std::runtime_error&) { cancelled = true; }
    });
    while (!entered) std::this_thread::yield();
    std::this_thread::sleep_for(20ms);
    const auto before = monotonic_ns();
    network.request_stop();
    network.request_stop();
    worker.join();
    require(cancelled && monotonic_ns() - before < 1000000000LL, "request_stop must promptly interrupt peer resolution");
    require(network.address() == "127.0.0.1", "cancellation must retain the link for synchronization shutdown");
    network.stop();
    require(network.address().empty(), "explicit stop releases the retained link");
}
#endif
}
int main() {
    try {
        existing_lifetime();
        startup_validation();
#ifdef __linux__
        discovery_lifetime();
        contract_check();
        duplicate_identity_check();
        stop_cancels_resolution();
        cancellation_retains_link();
#endif
        std::cout << "Network session checks passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
