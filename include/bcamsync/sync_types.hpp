#pragma once
#include "timing.hpp"
#include <cstdint>
#include <string>
#include <vector>

namespace bcamsync {
struct NodeConfig {
    std::string id, address, interface;
    std::uint16_t port = 58080;
};
struct SyncUnitConfig {
    std::string id, node_id;
};
/// Group membership, cadence, transport, and clock quality independent of acquisition.
struct SyncConfig {
    std::string group_id, reference_unit;
    unsigned fps = 40;
    std::string network_mode = "auto", clock_mode = "network";
    std::uint16_t clock_port = 58081;
    std::vector<NodeConfig> nodes;
    std::vector<SyncUnitConfig> units;
    double clock_max_offset_ms = 0.1, clock_max_age_seconds = 10.0;
    double request_timeout_seconds = 2.0, startup_timeout_seconds = 60.0;
    void validate() const;
    const NodeConfig& node(const std::string& id) const;
    const SyncUnitConfig& unit(const std::string& id) const;
    std::string reference_node() const;
    static SyncConfig from_file(const std::string& path);
    std::string to_json() const;
    /// Canonical shared contract; local transport settings and quality thresholds are omitted.
    std::string sync_json() const;
};
/// Add offset_ns to native local timestamps; unavailable clocks map to zero.
struct ClockMapping {
    bool valid = false;
    std::int64_t offset_ns = 0, uncertainty_ns = 0;
    std::int64_t map(std::int64_t local_ns) const;
};
struct UnitStatus {
    std::string unit_id, state = "warming", fault;
    bool ready = false;
    double phase_ms = 0, p95_ms = 0, max_ms = 0;
    /// Zero until a settled phase window has been measured.
    std::int64_t measurement_end_ns = 0;
};
struct GroupStatus {
    std::string state = "stopped", fault;
    bool ready = false;
    std::vector<UnitStatus> units;
};
} // namespace bcamsync
