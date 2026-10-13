#pragma once
#include "timing.hpp"
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace bcamsync {
struct UnitConfig {
    std::string id, node_id, camera_id;
    unsigned width = 5120, height = 800, views = 4, fps = 40;
    /// Backend-owned sensor, raw packing, and pipeline selection.
    std::string profile = "arducam_b0267_pi4";
    int exposure_us = 10000;
    float analogue_gain = 1.0F;
    /// Calibrate sensor gain at startup, then lock it before phase synchronization.
    bool auto_gain = true;
    unsigned queue_capacity = 4, pool_capacity = 12;
    std::int64_t fake_initial_phase_us = 0;
    double fake_drift_ppm = 0;
};
struct NodeConfig {
    std::string id, address, interface;
    std::uint16_t port = 58080;
};
struct Config {
    std::string group_id, reference_unit;
    std::string backend = "arducam";
    std::vector<NodeConfig> nodes;
    std::vector<UnitConfig> units;
    double clock_max_offset_ms = 0.1, clock_max_age_seconds = 10.0;
    double request_timeout_seconds = 2.0, startup_timeout_seconds = 60.0;
    void validate() const;
    const NodeConfig& node(const std::string& id) const;
    const UnitConfig& unit(const std::string& id) const;
    std::string reference_node() const;
    static Config from_file(const std::string& path);
    /// Resolved acquisition configuration for optional management clients.
    std::string to_json() const;
};
struct FrameStorage {
    std::vector<std::uint8_t> pixels;
};
/// Views retain immutable packed storage; rows may have a wider stride than a view.
struct FramePacket {
    std::string unit_id;
    std::uint64_t sequence = 0, dropped_before = 0;
    std::int64_t sensor_timestamp_ns = 0, timestamp_ns = 0;
    int requested_exposure_us = 0, exposure_us = 0;
    float requested_gain = 0, analogue_gain = 0;
    bool gain_locked = false;
    std::string gain_state;
    unsigned width = 0, height = 0, views = 0;
    std::shared_ptr<const FrameStorage> storage;
    unsigned view_width() const;
    const std::uint8_t* view_data(unsigned index) const;
};
} // namespace bcamsync
