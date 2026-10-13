#pragma once
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace bcamsync {
struct FrameTiming {
    std::uint64_t camera_sequence = 0;
    std::int64_t sensor_timestamp_ns = 0, timestamp_ns = 0, mapping_span_ns = 0;
    std::int64_t requested_duration_us = 0, reported_duration_us = 0;
    std::uint64_t correction_id = 0;
};
struct CorrectionStatus {
    std::string state = "idle";
    std::uint64_t correction_id = 0;
    std::int64_t restored_timestamp_ns = 0;
};
struct TimingSnapshot {
    std::string unit_id;
    std::int64_t snapshot_timestamp_ns = 0;
    std::vector<FrameTiming> frames;
    CorrectionStatus correction;
};
struct Pulse {
    std::uint64_t correction_id = 0;
    int delta_us = 0, count = 0;
    std::int64_t expires_ns = 0;
};
struct UnitConfig {
    std::string id, node_id, camera_id;
    unsigned width = 5120, height = 800, views = 4, fps = 40;
    std::string raw_format = "R8";
    unsigned r16_shift = 8;
    int exposure_us = 10000;
    float analogue_gain = 1.0F;
    unsigned queue_capacity = 4, pool_capacity = 12;
    std::string pipeline_config;
};
struct NodeConfig {
    std::string id, address, interface;
    std::uint16_t port = 58080;
};
struct Config {
    std::string group_id, reference_unit;
    std::vector<NodeConfig> nodes;
    std::vector<UnitConfig> units;
    double clock_max_offset_ms = 0.1, clock_max_age_seconds = 10.0;
    double request_timeout_seconds = 2.0, startup_timeout_seconds = 60.0;
    void validate() const;
    const NodeConfig &node(const std::string &id) const;
    const UnitConfig &unit(const std::string &id) const;
    std::string reference_node() const;
    static Config from_file(const std::string &path);
};
struct FrameStorage { std::vector<std::uint8_t> pixels; };
/// Views retain immutable packed storage; rows may have a wider stride than a view.
struct FramePacket {
    std::string unit_id;
    std::uint64_t sequence = 0, dropped_before = 0;
    std::int64_t sensor_timestamp_ns = 0, timestamp_ns = 0;
    unsigned width = 0, height = 0, views = 0;
    std::shared_ptr<const FrameStorage> storage;
    unsigned view_width() const;
    const std::uint8_t *view_data(unsigned index) const;
};
std::int64_t monotonic_ns();
std::int64_t realtime_ns();
}
