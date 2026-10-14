#include "bcamsync/sync_types.hpp"
#include <algorithm>
#include <arpa/inet.h>
#include <cmath>
#include <fstream>
#include <limits>
#include <nlohmann/json.hpp>
#include <set>
#include <stdexcept>
#include <time.h>

namespace bcamsync {
namespace {
std::int64_t clock_value(clockid_t id) {
    timespec t{};
    if (clock_gettime(id, &t) != 0)
        throw std::runtime_error("clock_gettime failed");
    return static_cast<std::int64_t>(t.tv_sec) * 1000000000LL + t.tv_nsec;
}
unsigned unsigned_value(const nlohmann::json& object, const char* key, unsigned initial) {
    if (!object.contains(key)) return initial;
    const auto& value = object.at(key);
    if (!value.is_number_integer() ||
        (!value.is_number_unsigned() && value.get<std::int64_t>() < 0) ||
        value.get<std::uint64_t>() > std::numeric_limits<unsigned>::max())
        throw std::invalid_argument(std::string("invalid unsigned field: ") + key);
    return value.get<unsigned>();
}
} // namespace
std::int64_t monotonic_ns() { return clock_value(CLOCK_MONOTONIC); }
std::int64_t realtime_ns() { return clock_value(CLOCK_REALTIME); }
std::int64_t boot_time_ns() {
#ifdef CLOCK_BOOTTIME
    return clock_value(CLOCK_BOOTTIME);
#else
    return monotonic_ns();
#endif
}
std::int64_t ClockMapping::map(std::int64_t local_ns) const {
    if (!valid) return 0;
    if ((offset_ns > 0 && local_ns > std::numeric_limits<std::int64_t>::max() - offset_ns) ||
        (offset_ns < 0 && local_ns < std::numeric_limits<std::int64_t>::min() - offset_ns))
        throw std::overflow_error("clock mapping exceeds timestamp range");
    return local_ns + offset_ns;
}
const NodeConfig& SyncConfig::node(const std::string& id) const {
    for (const auto& n : nodes)
        if (n.id == id) return n;
    throw std::invalid_argument("unknown node: " + id);
}
const SyncUnitConfig& SyncConfig::unit(const std::string& id) const {
    for (const auto& u : units)
        if (u.id == id) return u;
    throw std::invalid_argument("unknown unit: " + id);
}
std::string SyncConfig::reference_node() const { return unit(reference_unit).node_id; }
void SyncConfig::validate() const {
    const auto valid_id = [](const std::string& id) {
        return !id.empty() && id.find('\0') == std::string::npos;
    };
    if (!valid_id(group_id) || nodes.empty() || units.empty())
        throw std::invalid_argument("invalid group identity or empty topology");
    if (network_mode != "auto" && network_mode != "existing")
        throw std::invalid_argument("network mode must be auto or existing");
    if (clock_mode != "network" && clock_mode != "host")
        throw std::invalid_argument("clock mode must be network or host");
    if (!clock_port || !fps || fps > 1000000)
        throw std::invalid_argument("invalid clock port or unrepresentable frame period");
    const bool addressed = nodes.size() > 1 && (network_mode == "existing" || clock_mode == "host");
    std::set<std::string> ids, addresses;
    for (const auto& n : nodes) {
        in_addr address{};
        if (!valid_id(n.id) || !ids.insert(n.id).second || !n.port ||
            n.interface.find('\0') != std::string::npos ||
            (clock_mode == "network" && nodes.size() > 1 && network_mode == "auto" && n.interface.empty()) ||
            (addressed && (inet_pton(AF_INET, n.address.c_str(), &address) != 1 ||
                          !addresses.insert(n.address + ":" + std::to_string(n.port)).second)))
            throw std::invalid_argument("invalid or duplicate node endpoint");
    }
    ids.clear();
    unit(reference_unit);
    for (const auto& u : units) {
        node(u.node_id);
        if (!valid_id(u.id) || !ids.insert(u.id).second)
            throw std::invalid_argument("invalid or duplicate synchronization unit");
    }
    for (const auto& n : nodes)
        if (std::none_of(units.begin(), units.end(), [&](const auto& u) { return u.node_id == n.id; }))
            throw std::invalid_argument("node has no units: " + n.id);
    if (!std::isfinite(clock_max_offset_ms) || clock_max_offset_ms <= 0 ||
        static_cast<long double>(clock_max_offset_ms) * 1e6L >= std::numeric_limits<std::int64_t>::max())
        throw std::invalid_argument("invalid clock offset threshold");
    const auto available = std::numeric_limits<std::int64_t>::max() - monotonic_ns();
    for (double value : {clock_max_age_seconds, request_timeout_seconds, startup_timeout_seconds})
        if (!std::isfinite(value) || value <= 0 || static_cast<long double>(value) * 1e9L >= available)
            throw std::invalid_argument("timeout is invalid or exceeds timestamp range");
    const long double idle_seconds = 2 + static_cast<long double>(request_timeout_seconds) * (nodes.size() + 1);
    if (idle_seconds * 1e9L >= available)
        throw std::invalid_argument("group request budget exceeds timestamp range");
}
SyncConfig SyncConfig::from_file(const std::string& path) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error("cannot open synchronization config: " + path);
    nlohmann::json document;
    input >> document;
    SyncConfig config;
    config.group_id = document.at("group_id");
    config.reference_unit = document.at("reference_unit");
    config.fps = unsigned_value(document, "fps", config.fps);
    config.clock_mode = document.value("clock_mode", config.clock_mode);
    if (document.contains("network")) {
        const auto& network = document.at("network");
        config.network_mode = network.value("mode", config.network_mode);
        const auto port = unsigned_value(network, "clock_port", config.clock_port);
        if (!port || port > 65535) throw std::invalid_argument("invalid clock port");
        config.clock_port = static_cast<std::uint16_t>(port);
    }
    for (const auto& value : document.at("nodes")) {
        NodeConfig node;
        node.id = value.at("id");
        node.address = value.value("address", std::string{});
        node.interface = value.value("interface", std::string{});
        const auto port = unsigned_value(value, "port", node.port);
        if (!port || port > 65535) throw std::invalid_argument("invalid node port");
        node.port = static_cast<std::uint16_t>(port);
        config.nodes.push_back(std::move(node));
    }
    for (const auto& value : document.at("units"))
        config.units.push_back({value.at("id"), value.at("node_id")});
#define SET(name) config.name = document.value(#name, config.name)
    SET(clock_max_offset_ms);
    SET(clock_max_age_seconds);
    SET(request_timeout_seconds);
    SET(startup_timeout_seconds);
#undef SET
    config.validate();
    return config;
}
std::string SyncConfig::to_json() const {
    validate();
    nlohmann::json node_values = nlohmann::json::array(), unit_values = nlohmann::json::array();
    for (const auto& node : nodes)
        node_values.push_back({{"id", node.id}, {"address", node.address},
                               {"interface", node.interface}, {"port", node.port}});
    for (const auto& unit : units)
        unit_values.push_back({{"id", unit.id}, {"node_id", unit.node_id}});
    return nlohmann::json({{"group_id", group_id}, {"reference_unit", reference_unit}, {"fps", fps},
        {"clock_mode", clock_mode}, {"network", {{"mode", network_mode}, {"clock_port", clock_port}}},
        {"nodes", node_values}, {"units", unit_values}, {"clock_max_offset_ms", clock_max_offset_ms},
        {"clock_max_age_seconds", clock_max_age_seconds}, {"request_timeout_seconds", request_timeout_seconds},
        {"startup_timeout_seconds", startup_timeout_seconds}}).dump();
}
std::string SyncConfig::sync_json() const {
    validate();
    std::vector<std::string> node_ids;
    for (const auto& node : nodes) node_ids.push_back(node.id);
    std::sort(node_ids.begin(), node_ids.end());
    std::vector<const SyncUnitConfig*> ordered;
    for (const auto& unit : units) ordered.push_back(&unit);
    std::sort(ordered.begin(), ordered.end(), [](const auto* left, const auto* right) { return left->id < right->id; });
    nlohmann::json unit_values = nlohmann::json::array();
    for (const auto* unit : ordered) unit_values.push_back({{"id", unit->id}, {"node_id", unit->node_id}});
    return nlohmann::json({{"group_id", group_id}, {"reference_unit", reference_unit}, {"fps", fps},
        {"clock_mode", clock_mode}, {"clock_domain", nodes.size() == 1 ? "local" : "common"},
        {"nodes", node_ids}, {"units", unit_values}}).dump();
}
} // namespace bcamsync
