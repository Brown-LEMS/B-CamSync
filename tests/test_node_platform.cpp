#include "platform.hpp"
#include "network_address.hpp"
#include <atomic>
#include <chrono>
#include <fstream>
#include <iostream>
#include <set>
#include <stdexcept>
#include <unistd.h>

using namespace bcamsync;
using namespace bcamsync::detail;
namespace {
void require(bool condition, const char* message) {
    if (!condition)
        throw std::runtime_error(message);
}
SyncConfig configuration() {
    SyncConfig config;
    config.group_id = "platform-test";
    config.reference_unit = "board-a";
    config.network_mode = "existing";
    config.nodes = {{"node-a", "10.77.0.1", "eth0", 58080},
                    {"node-b", "10.77.0.2", "eth0", 58080}};
    SyncUnitConfig a, b;
    a.id = "board-a";
    a.node_id = "node-a";
    b.id = "board-b";
    b.node_id = "node-b";
    config.units = {a, b};
    return config;
}
std::string report(const std::string& correction, const std::string& reference_time = "1005",
                   const std::string& dispersion = "0.000010000",
                   const std::string& leap = "Normal", const std::string& last = "0.000010000") {
    return "0A4D0001,10.77.0.1,9," + reference_time + ',' + correction + ',' + last +
           ",0.000010000,0.000,0.000,0.010,0.000020000," + dispersion + ",1.0," + leap + '\n';
}
void mapping() {
    const auto config = configuration();
    // The local realtime clock is five seconds slow; chronyd -x does not step it.
    auto estimate = tracking_estimate(config, "node-b", report("5.000000000"),
                                       1000000000000LL, 10000000000LL, 100);
    require(estimate.valid, "large correctable system offset must remain usable");
    require(estimate.offset_ns == 995000000000LL, "positive correction adds group time");
    require(estimate.uncertainty_ns == 20100, "bound includes root distance and local mapping");
    estimate = tracking_estimate(config, "node-b", report("-5.000000000", "995"),
                                 1000000000000LL, 10000000000LL);
    require(estimate.valid && estimate.offset_ns == 985000000000LL,
            "negative correction subtracts group time");
    require(!tracking_estimate(config, "node-b", report("5", "980"),
                               1000000000000LL, 10000000000LL).valid,
            "stale group reference must suspend clock mapping");
    require(!tracking_estimate(config, "node-b", report("5", "1005", "0.001"),
                               1000000000000LL, 10000000000LL).valid,
            "root uncertainty must gate readiness");
    require(!tracking_estimate(config, "node-b", report("5", "1005", "0.00001", "Normal", "0.001"),
                               1000000000000LL, 10000000000LL).valid,
            "unsettled residual offset must gate readiness");
    require(!tracking_estimate(config, "node-b", report("5", "1005", "0.00001", "Not synchronised"),
                               1000000000000LL, 10000000000LL).valid,
            "unsynchronized clock must be invalid");
    require(!tracking_estimate(config, "node-b", report("nan"),
                               1000000000000LL, 10000000000LL).valid,
            "nonfinite correction must be rejected");
    require(!tracking_estimate(config, "node-b", "broken report", 0, 0).valid,
            "malformed tracking report must be rejected");
    require(!tracking_estimate(config, "node-a", report("0"),
                               1000000000000LL, 10000000000LL).valid,
            "reference must use the private local clock");
    const auto local = tracking_estimate(config, "node-a",
        "7F7F0101,,8,0,0,0,0,0,0,0,0,0,0,Normal\n", 1000000000000LL, 10000000000LL, 100);
    require(local.valid && local.offset_ns == 990000000000LL && local.uncertainty_ns == 100,
            "local reference mapping uses its local realtime clock");
    auto relaxed = config;
    relaxed.clock_max_offset_ms = 2;
    require(tracking_estimate(relaxed, "node-b", report("5", "1005", "0.001"),
                              1000000000000LL, 10000000000LL).valid,
            "configured quality threshold must not have a hardcoded 0.1 ms ceiling");
}
void private_configuration() {
    auto config = configuration();
    config.clock_port = 59001;
    const auto reference = clock_config(config, "node-a", "/tmp/private-clock");
    const auto follower = clock_config(config, "node-b", "/tmp/private-clock");
    require(reference.find("port 59001\n") != std::string::npos,
            "reference uses configured unprivileged NTP port");
    require(reference.find("bindaddress 10.77.0.1") != std::string::npos &&
            reference.find("allow 10.77.0.2/32") != std::string::npos,
            "private reference serves only configured nodes");
    require(follower.find("server 10.77.0.1 port 59001") != std::string::npos &&
            follower.find("bindacqaddress 10.77.0.2") != std::string::npos,
            "follower selects the configured reference and acquisition address");
    for (const auto& text : {reference, follower}) {
        require(text.find("pidfile /tmp/private-clock/chronyd.pid") != std::string::npos &&
                text.find("bindcmdaddress /tmp/private-clock/chronyd.sock\ncmdport 0") != std::string::npos,
                "clock instance owns private process state and Unix command socket");
        require(text.find("makestep") == std::string::npos &&
                text.find("rtcsync") == std::string::npos &&
                text.find("/etc/") == std::string::npos,
                "clock configuration never provisions system clock state");
    }
    config.reference_unit = "board-b";
    require(clock_config(config, "node-a", "/tmp/another-clock").find("server 10.77.0.2") != std::string::npos,
            "reference selection changes per instance without a deployment registry");
}
void link_startup_validation() {
    bool rejected = false;
    try { NetworkLink invalid("", "test"); }
    catch (const std::invalid_argument&) { rejected = true; }
    require(rejected, "network activation requires an explicit interface");
    NetworkLink link("eth0", "link-test");
    std::atomic<bool> stopping{true};
    rejected = false;
    try { link.start(stopping, monotonic_ns() + 1000000000LL); }
    catch (const std::runtime_error&) { rejected = true; }
    require(rejected, "cancelled activation must not contact NetworkManager");
    stopping = false;
    rejected = false;
    try { link.start(stopping, monotonic_ns() - 1); }
    catch (const std::runtime_error&) { rejected = true; }
    require(rejected, "expired activation must not contact NetworkManager");
    link.stop();
}
#ifdef __linux__
struct InterfaceAddress {
    ifaddrs entry{};
    sockaddr_in address{}, broadcast{};
    explicit InterfaceAddress(const char* value, const char* interface = "eth0") {
        address.sin_family = AF_INET;
        require(inet_pton(AF_INET, value, &address.sin_addr) == 1, "valid address fixture");
        entry.ifa_name = const_cast<char*>(interface);
        entry.ifa_flags = IFF_UP | IFF_RUNNING;
        entry.ifa_addr = reinterpret_cast<sockaddr*>(&address);
    }
};
void address_selection() {
    require(select_link_address(nullptr, "eth0").empty(), "no address requires link-local activation");
    InterfaceAddress assigned("192.168.1.20"), local("169.254.2.3"), lower("192.168.1.10");
    local.entry.ifa_next = &assigned.entry;
    assigned.entry.ifa_next = &lower.entry;
    require(select_link_address(&local.entry, "eth0") == "192.168.1.10",
            "existing ordinary IPv4 takes priority with deterministic numeric ordering");
    require(select_link_address(&local.entry, "eth0", true) == "169.254.2.3",
            "owned link activation waits specifically for a link-local address");
    require(select_link_address(&lower.entry, "eth0", true).empty(),
            "ordinary addresses do not complete link-local activation");
    require(select_link_address(&local.entry, "other").empty(), "only the configured interface is reused");
    assigned.entry.ifa_next = &local.entry;
    local.entry.ifa_next = nullptr;
    require(select_link_address(&assigned.entry, "eth0") == "192.168.1.20",
            "address order cannot make link-local win over ordinary IPv4");
    require(select_link_address(&local.entry, "eth0") == "169.254.2.3",
            "an existing link-local address is reusable");
    for (const auto value : {"0.0.0.0", "0.1.2.3", "127.0.0.1", "239.1.2.3", "255.255.255.255"}) {
        InterfaceAddress invalid(value);
        require(select_link_address(&invalid.entry, "eth0").empty(), "non-unicast addresses are unusable");
    }
    InterfaceAddress unavailable("10.0.0.1");
    for (const unsigned flags : {0U, static_cast<unsigned>(IFF_UP), static_cast<unsigned>(IFF_RUNNING),
                                static_cast<unsigned>(IFF_UP | IFF_RUNNING | IFF_LOOPBACK)}) {
        unavailable.entry.ifa_flags = flags;
        require(select_link_address(&unavailable.entry, "eth0").empty(),
                "down, disconnected, and loopback interfaces are not reused");
    }
    unavailable.entry.ifa_flags = IFF_UP | IFF_RUNNING | IFF_BROADCAST;
    unavailable.broadcast = unavailable.address;
    unavailable.entry.ifa_broadaddr = reinterpret_cast<sockaddr*>(&unavailable.broadcast);
    require(select_link_address(&unavailable.entry, "eth0").empty(), "broadcast addresses are not reused");
    unavailable.entry.ifa_broadaddr = nullptr;
    unavailable.address.sin_family = AF_INET6;
    require(select_link_address(&unavailable.entry, "eth0").empty(), "IPv6 does not satisfy IPv4 startup");
    unavailable.entry.ifa_addr = nullptr;
    require(select_link_address(&unavailable.entry, "eth0").empty(), "entries without addresses are ignored");
}
#endif
void software_lifecycle() {
    auto config = configuration();
    config.clock_mode = "host";
    config.network_mode = "auto";
    config.nodes[0].address = "127.0.0.1";
    config.nodes[1].address = "127.0.0.1";
    config.nodes[1].port = 58082;
    NodePlatform platform(config, "node-a");
    std::atomic<bool> stopping{false};
    require(!platform.sample().valid, "unstarted platform has no clock mapping");
    platform.start(config, stopping, monotonic_ns() + 1000000000LL);
    const auto estimate = platform.sample();
    require(estimate.valid && estimate.offset_ns > 1000000000000LL,
            "same-host nodes map boottime into realtime without provisioning");
    platform.stop();
    require(!platform.sample().valid, "stop invalidates mapping");
    platform.start(config, stopping, monotonic_ns() + 1000000000LL);
    platform.stop();
    config.nodes.resize(1);
    config.units.resize(1);
    config.clock_mode = "network";
    NodePlatform single(config, "node-a");
    single.start(config, stopping, monotonic_ns() + 1000000000LL);
    require(single.sample().valid && single.sample().offset_ns == 0,
            "single physical node keeps its native boottime domain without dependencies");
}
void private_clock_smoke() {
    auto config = configuration();
    config.nodes[0].address = "127.0.0.1";
    config.nodes[1].address = "127.0.0.2";
    config.clock_max_offset_ms = 10;
    NodePlatform reference(config, "node-a"), follower(config, "node-b");
    std::atomic<bool> stopping{false};
#ifdef __linux__
    const auto children = [] {
        std::ifstream input("/proc/self/task/" + std::to_string(getpid()) + "/children");
        require(input.good(), "Linux child ownership report must be readable");
        std::set<int> result;
        int pid{};
        while (input >> pid)
            result.insert(pid);
        return result;
    };
    const auto before = children();
#endif
    const auto deadline = monotonic_ns() + 20000000000LL;
    reference.start(config, stopping, deadline);
    follower.start(config, stopping, deadline);
    require(reference.sample().valid && follower.sample().valid,
            "private chronyd instances must communicate over configured high port");
#ifdef __linux__
    require(children().size() == before.size() + 2, "platform must own both private clock children");
#endif
    follower.stop();
    reference.stop();
    require(!follower.sample().valid && !reference.sample().valid,
            "clock process shutdown invalidates the estimators");
#ifdef __linux__
    require(children() == before, "stop must terminate and reap owned chronyd children");
#endif
    std::cout << "Private chronyd loopback software check passed; no device or optical measurement\n";
}
} // namespace
int main(int argc, char** argv) {
    try {
        mapping();
        private_configuration();
        link_startup_validation();
#ifdef __linux__
        address_selection();
#endif
        software_lifecycle();
        if (argc == 2 && std::string(argv[1]) == "--private-clock")
            private_clock_smoke();
        std::cout << "Node platform software checks passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
