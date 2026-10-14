#include "bcamsync/types.hpp"
#include "phase_policy_json.hpp"
#include "arducam_profile.hpp"
#include <algorithm>
#include <cmath>
#include <fstream>
#include <limits>
#include <nlohmann/json.hpp>
#include <set>
#include <stdexcept>
namespace bcamsync {
namespace {
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
const NodeConfig& Config::node(const std::string& id) const {
    for (const auto& n : nodes)
        if (n.id == id)
            return n;
    throw std::invalid_argument("unknown node: " + id);
}
const UnitConfig& Config::unit(const std::string& id) const {
    for (const auto& u : units)
        if (u.id == id)
            return u;
    throw std::invalid_argument("unknown unit: " + id);
}
std::string Config::reference_node() const { return unit(reference_unit).node_id; }
SyncConfig Config::sync_config() const {
    SyncConfig config;
    config.group_id = group_id;
    config.reference_unit = reference_unit;
    config.fps = fps;
    config.network_mode = network_mode;
    config.clock_mode = clock_mode;
    config.clock_port = clock_port;
    config.nodes = nodes;
    for (const auto& unit : units) config.units.push_back({unit.id, unit.node_id});
    config.phase_policy = phase_policy;
    config.clock_max_offset_ms = clock_max_offset_ms;
    config.clock_max_age_seconds = clock_max_age_seconds;
    config.request_timeout_seconds = request_timeout_seconds;
    config.startup_timeout_seconds = startup_timeout_seconds;
    return config;
}
void Config::validate() const {
    sync_config().validate();
    if (backend != "arducam" && backend != "fake")
        throw std::invalid_argument("unknown acquisition backend: " + backend);
    std::set<std::string> devices;
    for (const auto& u : units) {
        if (u.camera_id.empty() || u.camera_id.find('\0') != std::string::npos ||
            !devices.insert(u.node_id + '\0' + u.camera_id).second)
            throw std::invalid_argument("invalid or duplicate camera identity");
        if (u.exposure_us <= 0 || u.exposure_us >= 1000000 / static_cast<int>(fps) ||
            !std::isfinite(u.analogue_gain) || u.analogue_gain <= 0 || !u.queue_capacity ||
            static_cast<std::uint64_t>(u.pool_capacity) <= u.queue_capacity)
            throw std::invalid_argument("invalid capture settings for " + u.id);
        unsigned width = detail::arducam_width, height = detail::arducam_height;
        if (backend == "arducam") {
            detail::arducam_pipeline(u.profile);
            if (u.exposure_us >= 1000000 / static_cast<int>(fps) -
                                     std::max(phase_policy.startup_delta_us, phase_policy.tracking_delta_us))
                throw std::invalid_argument("exposure leaves no phase correction margin for " + u.id);
        } else {
            width = u.fake.width;
            height = u.fake.height;
            if (!width || !height || width % 4 || u.fake.initial_phase_us < -1000000 ||
                u.fake.initial_phase_us > 1000000 || !std::isfinite(u.fake.drift_ppm) ||
                std::abs(u.fake.drift_ppm) > 1000)
                throw std::invalid_argument("invalid fake geometry or timing for " + u.id);
        }
        const auto pixels = static_cast<std::uint64_t>(width) * height;
        if (pixels > std::vector<std::uint8_t>{}.max_size() ||
            pixels > std::numeric_limits<std::size_t>::max() / u.pool_capacity)
            throw std::invalid_argument("frame storage extent is not representable for " + u.id);
    }
}
Config Config::from_file(const std::string& path) {
    std::ifstream f(path);
    if (!f)
        throw std::runtime_error("cannot open config: " + path);
    nlohmann::json j;
    f >> j;
    Config c;
    c.group_id = j.at("group_id").get<std::string>();
    c.reference_unit = j.at("reference_unit").get<std::string>();
    c.backend = j.value("backend", c.backend);
    c.clock_mode = j.value("clock_mode", c.clock_mode);
    if (j.contains("phase_policy"))
        c.phase_policy = detail::phase_policy_from_json(j.at("phase_policy"));
    c.fps = unsigned_value(j, "fps", c.fps);
    if (j.contains("network")) {
        const auto& network = j.at("network");
        c.network_mode = network.value("mode", c.network_mode);
        const auto port = unsigned_value(network, "clock_port", c.clock_port);
        if (!port || port > 65535) throw std::invalid_argument("invalid clock port");
        c.clock_port = static_cast<std::uint16_t>(port);
    }
    for (const auto& v : j.at("nodes")) {
        NodeConfig n;
        n.id = v.at("id");
        n.address = v.value("address", std::string{});
        n.interface = v.value("interface", std::string{});
        const auto port = unsigned_value(v, "port", n.port);
        if (port < 1 || port > 65535)
            throw std::invalid_argument("invalid node port");
        n.port = static_cast<std::uint16_t>(port);
        c.nodes.push_back(n);
    }
    for (const auto& v : j.at("units")) {
        UnitConfig u;
        u.id = v.at("id");
        u.node_id = v.at("node_id");
        u.camera_id = v.at("camera_id");
#define SET(name) u.name = v.value(#name, u.name)
        if (v.contains("width") || v.contains("height") || v.contains("views") || v.contains("fps") ||
            v.contains("raw_format") || v.contains("r16_shift") || v.contains("pipeline_config"))
            throw std::invalid_argument("geometry is profile-owned; fps belongs to the group");
        SET(profile);
        SET(exposure_us);
        SET(analogue_gain);
        SET(auto_gain);
        u.queue_capacity = unsigned_value(v, "queue_capacity", u.queue_capacity);
        u.pool_capacity = unsigned_value(v, "pool_capacity", u.pool_capacity);
#undef SET
        if (v.contains("fake")) {
            const auto& fake = v.at("fake");
            u.fake.width = unsigned_value(fake, "width", u.fake.width);
            u.fake.height = unsigned_value(fake, "height", u.fake.height);
            u.fake.initial_phase_us = fake.value("initial_phase_us", u.fake.initial_phase_us);
            u.fake.drift_ppm = fake.value("drift_ppm", u.fake.drift_ppm);
        }
        c.units.push_back(u);
    }
#define SET(name) c.name = j.value(#name, c.name)
    SET(clock_max_offset_ms);
    SET(clock_max_age_seconds);
    SET(request_timeout_seconds);
    SET(startup_timeout_seconds);
#undef SET
    c.validate();
    return c;
}
std::string Config::to_json() const {
    validate();
    nlohmann::json ns = nlohmann::json::array(), us = nlohmann::json::array();
    for (const auto& n : nodes)
        ns.push_back({{"id", n.id}, {"address", n.address}, {"interface", n.interface}, {"port", n.port}});
    for (const auto& u : units) {
        nlohmann::json unit = {{"id", u.id}, {"node_id", u.node_id}, {"camera_id", u.camera_id},
            {"profile", u.profile}, {"exposure_us", u.exposure_us}, {"analogue_gain", u.analogue_gain},
            {"auto_gain", u.auto_gain}, {"queue_capacity", u.queue_capacity}, {"pool_capacity", u.pool_capacity}};
        if (backend == "fake")
            unit["fake"] = {{"width", u.fake.width}, {"height", u.fake.height},
                            {"initial_phase_us", u.fake.initial_phase_us}, {"drift_ppm", u.fake.drift_ppm}};
        us.push_back(std::move(unit));
    }
    return nlohmann::json({{"group_id", group_id}, {"reference_unit", reference_unit}, {"backend", backend},
        {"fps", fps}, {"clock_mode", clock_mode}, {"network", {{"mode", network_mode}, {"clock_port", clock_port}}},
        {"nodes", ns}, {"units", us}, {"phase_policy", detail::phase_policy_json(phase_policy)},
        {"clock_max_offset_ms", clock_max_offset_ms},
        {"clock_max_age_seconds", clock_max_age_seconds}, {"request_timeout_seconds", request_timeout_seconds},
        {"startup_timeout_seconds", startup_timeout_seconds}}).dump();
}
std::string Config::sync_json() const { return sync_config().sync_json(); }
const std::uint8_t* FramePacket::view_data(unsigned index) const {
    if (!storage || !views || !width || !height || width % views || index >= views ||
        storage->pixels.size() != static_cast<std::uint64_t>(width) * height)
        throw std::out_of_range("invalid frame view or storage");
    return storage->pixels.data() + static_cast<std::size_t>(index) * view_width();
}
unsigned FramePacket::view_width() const {
    if (!views || !width || width % views)
        throw std::invalid_argument("invalid frame view layout");
    return width / views;
}
} // namespace bcamsync
