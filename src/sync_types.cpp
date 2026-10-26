#include "bcamsync/sync_types.hpp"
#include "bcamsync/detail/phase_policy_json.hpp"
#include "config_ffi.hpp"
#include <cmath>
#include <fstream>
#include <memory>
#include <nlohmann/json.hpp>
#include <stdexcept>

namespace bcamsync {
namespace {
using Json = nlohmann::json;
Json checked(char* value) {
    if (!value) throw std::runtime_error("Rust configuration returned no result");
    std::unique_ptr<char, decltype(&bcamsync_rust_string_free)> owner(value, bcamsync_rust_string_free);
    auto response = Json::parse(owner.get());
    if (response.contains("error")) {
        if (response.value("kind", 1) == 4) throw std::overflow_error(response.at("error").get<std::string>());
        throw std::invalid_argument(response.at("error").get<std::string>());
    }
    return response.at("ok");
}
Json document(const SyncConfig& config) {
    if (config.join_timeout_seconds && !std::isfinite(*config.join_timeout_seconds))
        throw std::invalid_argument("join timeout must be finite and positive");
    Json nodes = Json::array(), units = Json::array();
    for (const auto& n : config.nodes) nodes.push_back({{"id",n.id},{"address",n.address},{"interface",n.interface},{"port",n.port}});
    for (const auto& u : config.units) units.push_back({{"id",u.id},{"node_id",u.node_id}});
    return {{"group_id",config.group_id},{"reference_unit",config.reference_unit},{"fps",config.fps},
        {"clock_mode",config.clock_mode},{"network",{{"mode",config.network_mode},{"clock_port",config.clock_port}}},
        {"nodes",nodes},{"units",units},{"phase_policy",detail::phase_policy_json(config.phase_policy)},
        {"clock_max_offset_ms",config.clock_max_offset_ms},{"clock_max_age_seconds",config.clock_max_age_seconds},
        {"request_timeout_seconds",config.request_timeout_seconds},{"startup_timeout_seconds",config.startup_timeout_seconds},
        {"join_timeout_seconds",config.join_timeout_seconds ? Json(*config.join_timeout_seconds) : Json(nullptr)}};
}
std::int64_t clock(int kind) {
    std::int64_t value{};
    if (!bcamsync_rust_clock_ns(kind, &value)) throw std::runtime_error("clock_gettime failed");
    return value;
}
}
namespace detail {
nlohmann::json phase_policy_json(const PhasePolicy& p) {
#define FIELD(name) {#name, p.name}
    return {FIELD(window_seconds),FIELD(evaluation_seconds),FIELD(freshness_seconds),FIELD(deadband_ms),
        FIELD(ready_p95_ms),FIELD(ready_max_ms),FIELD(ready_windows),FIELD(startup_delta_us),FIELD(startup_frames),
        FIELD(tracking_delta_us),FIELD(tracking_frames),FIELD(correction_interval_seconds),FIELD(settling_seconds),
        FIELD(application_delay_seconds),FIELD(startup_max_corrections),FIELD(excursion_ms),FIELD(excursion_timeout_seconds),
        FIELD(no_progress_corrections),FIELD(progress_ms),FIELD(gain),FIELD(max_mapping_span_ns),FIELD(max_samples)};
#undef FIELD
}
PhasePolicy phase_policy_from_json(const nlohmann::json& input) {
    const auto value = checked(bcamsync_rust_config(input.dump().c_str(), 2));
    PhasePolicy p;
#define READ(name) p.name = value.at(#name).get<decltype(p.name)>()
    READ(window_seconds);READ(evaluation_seconds);READ(freshness_seconds);READ(deadband_ms);READ(ready_p95_ms);
    READ(ready_max_ms);READ(ready_windows);READ(startup_delta_us);READ(startup_frames);READ(tracking_delta_us);
    READ(tracking_frames);READ(correction_interval_seconds);READ(settling_seconds);READ(application_delay_seconds);
    READ(startup_max_corrections);READ(excursion_ms);READ(excursion_timeout_seconds);READ(no_progress_corrections);
    READ(progress_ms);READ(gain);READ(max_mapping_span_ns);READ(max_samples);
#undef READ
    return p;
}
}
std::int64_t monotonic_ns() { return clock(0); }
std::int64_t boot_time_ns() { return clock(1); }
std::int64_t realtime_ns() { return clock(2); }
std::int64_t ClockMapping::map(std::int64_t local) const {
    return checked(bcamsync_rust_map_time(valid,offset_ns,local)).get<std::int64_t>();
}
const NodeConfig& SyncConfig::node(const std::string& id) const {
    for (const auto& n : nodes) if (n.id == id) return n;
    throw std::invalid_argument("unknown node: " + id);
}
const SyncUnitConfig& SyncConfig::unit(const std::string& id) const {
    for (const auto& u : units) if (u.id == id) return u;
    throw std::invalid_argument("unknown unit: " + id);
}
std::string SyncConfig::reference_node() const { return unit(reference_unit).node_id; }
void SyncConfig::validate() const { checked(bcamsync_rust_config(document(*this).dump().c_str(), 0)); }
std::string SyncConfig::to_json() const { return checked(bcamsync_rust_config(document(*this).dump().c_str(), 0)).dump(); }
std::string SyncConfig::sync_json() const { return checked(bcamsync_rust_config(document(*this).dump().c_str(), 1)).dump(); }
SyncConfig SyncConfig::from_file(const std::string& path) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error("cannot open synchronization config: " + path);
    Json raw; input >> raw;
    const auto value = checked(bcamsync_rust_config(raw.dump().c_str(), 0));
    SyncConfig config;
    config.group_id = value.at("group_id"); config.reference_unit = value.at("reference_unit");
    config.fps = value.at("fps"); config.clock_mode = value.at("clock_mode");
    config.network_mode = value.at("network").at("mode"); config.clock_port = value.at("network").at("clock_port");
    for (const auto& n : value.at("nodes")) config.nodes.push_back({n.at("id"),n.at("address"),n.at("interface"),n.at("port")});
    for (const auto& u : value.at("units")) config.units.push_back({u.at("id"),u.at("node_id")});
    config.phase_policy = detail::phase_policy_from_json(value.at("phase_policy"));
    config.clock_max_offset_ms = value.at("clock_max_offset_ms"); config.clock_max_age_seconds = value.at("clock_max_age_seconds");
    config.request_timeout_seconds = value.at("request_timeout_seconds"); config.startup_timeout_seconds = value.at("startup_timeout_seconds");
    if (!value.at("join_timeout_seconds").is_null())
        config.join_timeout_seconds = value.at("join_timeout_seconds").get<double>();
    return config;
}
} // namespace bcamsync
