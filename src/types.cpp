#include "bcamsync/types.hpp"
#include "arducam_profile.hpp"
#include <algorithm>
#include <arpa/inet.h>
#include <cmath>
#include <fstream>
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
} // namespace
std::int64_t monotonic_ns() { return clock_value(CLOCK_MONOTONIC); }
std::int64_t realtime_ns() { return clock_value(CLOCK_REALTIME); }
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
void Config::validate() const {
    const auto valid_id = [](const std::string& s) {
        const auto alphanumeric = [](unsigned char x) {
            return (x >= 'a' && x <= 'z') || (x >= 'A' && x <= 'Z') || (x >= '0' && x <= '9');
        };
        return !s.empty() && s.size() <= 64 &&
               alphanumeric(static_cast<unsigned char>(s.front())) &&
               std::all_of(s.begin(), s.end(), [](unsigned char x) {
                   return (x >= 'a' && x <= 'z') || (x >= 'A' && x <= 'Z') ||
                          (x >= '0' && x <= '9') || x == '-' || x == '_';
               });
    };
    if (!valid_id(group_id) || nodes.empty() || nodes.size() > 32 || units.empty() ||
        units.size() > 64)
        throw std::invalid_argument("invalid group identity or topology size");
    if (backend != "arducam" && backend != "fake")
        throw std::invalid_argument("unknown acquisition backend: " + backend);
    std::set<std::string> ids, addresses, devices;
    for (const auto& n : nodes) {
        in_addr a{};
        if (!valid_id(n.id) || !ids.insert(n.id).second || !n.port ||
            inet_pton(AF_INET, n.address.c_str(), &a) != 1 ||
            !addresses.insert(n.address + ":" + std::to_string(n.port)).second)
            throw std::invalid_argument("invalid or duplicate node endpoint");
    }
    ids.clear();
    const auto& reference = unit(reference_unit);
    for (const auto& u : units) {
        node(u.node_id);
        if (!valid_id(u.id) || !ids.insert(u.id).second || u.camera_id.empty() ||
            !devices.insert(u.node_id + "/" + u.camera_id).second)
            throw std::invalid_argument("invalid or duplicate unit identity");
        if (!u.width || !u.height || u.views != 4 || u.width % u.views ||
            static_cast<std::uint64_t>(u.width) * u.height > 64 * 1024 * 1024 || !u.fps ||
            u.fps > 45 || u.fps != reference.fps || u.exposure_us <= 0 ||
            u.exposure_us >= 1000000 / static_cast<int>(u.fps) - 1000 ||
            !std::isfinite(u.analogue_gain) || u.analogue_gain <= 0 || u.queue_capacity < 1 ||
            u.queue_capacity > 64 || u.pool_capacity < u.queue_capacity + 2 ||
            u.pool_capacity > 128)
            throw std::invalid_argument("invalid capture settings for " + u.id);
        if (backend == "arducam") detail::arducam_pipeline(u.profile);
        if (u.fake_initial_phase_us < -1000000 || u.fake_initial_phase_us > 1000000 ||
            !std::isfinite(u.fake_drift_ppm) || std::abs(u.fake_drift_ppm) > 1000)
            throw std::invalid_argument("invalid fake timing settings for " + u.id);
    }
    for (const auto& n : nodes)
        if (std::none_of(units.begin(), units.end(),
                         [&](const auto& u) { return u.node_id == n.id; }))
            throw std::invalid_argument("node has no units: " + n.id);
    for (double v : {clock_max_offset_ms, clock_max_age_seconds, request_timeout_seconds,
                     startup_timeout_seconds})
        if (!std::isfinite(v) || v <= 0 || v > 86400)
            throw std::invalid_argument("invalid timeout/clock setting");
    if (clock_max_offset_ms > 0.1 || request_timeout_seconds > 5 || startup_timeout_seconds > 600)
        throw std::invalid_argument("clock gate or timeout exceeds bounds");
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
    for (const auto& v : j.at("nodes")) {
        NodeConfig n;
        n.id = v.at("id");
        n.address = v.at("address");
        n.interface = v.value("interface", std::string{});
        const auto port = v.value("port", 58080);
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
        SET(width);
        SET(height);
        SET(views);
        SET(fps);
        if (v.contains("raw_format") || v.contains("r16_shift") || v.contains("pipeline_config"))
            throw std::invalid_argument("select a capture profile instead of raw/pipeline overrides");
        SET(profile);
        SET(exposure_us);
        SET(analogue_gain);
        SET(auto_gain);
        SET(queue_capacity);
        SET(pool_capacity);
#undef SET
        if (v.contains("fake")) {
            const auto& fake = v.at("fake");
            u.fake_initial_phase_us = fake.value("initial_phase_us", u.fake_initial_phase_us);
            u.fake_drift_ppm = fake.value("drift_ppm", u.fake_drift_ppm);
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
            {"width", u.width}, {"height", u.height}, {"views", u.views}, {"fps", u.fps},
            {"profile", u.profile}, {"exposure_us", u.exposure_us}, {"analogue_gain", u.analogue_gain},
            {"auto_gain", u.auto_gain}, {"queue_capacity", u.queue_capacity}, {"pool_capacity", u.pool_capacity}};
        if (backend == "fake")
            unit["fake"] = {{"initial_phase_us", u.fake_initial_phase_us}, {"drift_ppm", u.fake_drift_ppm}};
        us.push_back(std::move(unit));
    }
    return nlohmann::json({{"group_id", group_id}, {"reference_unit", reference_unit}, {"backend", backend},
        {"nodes", ns}, {"units", us}, {"clock_max_offset_ms", clock_max_offset_ms},
        {"clock_max_age_seconds", clock_max_age_seconds}, {"request_timeout_seconds", request_timeout_seconds},
        {"startup_timeout_seconds", startup_timeout_seconds}}).dump();
}
const std::uint8_t* FramePacket::view_data(unsigned index) const {
    if (!storage || !views || !width || !height || width % views || index >= views ||
        storage->pixels.size() != static_cast<std::size_t>(width) * height)
        throw std::out_of_range("invalid frame view or storage");
    return storage->pixels.data() + static_cast<std::size_t>(index) * view_width();
}
unsigned FramePacket::view_width() const {
    if (!views || !width || width % views)
        throw std::invalid_argument("invalid frame view layout");
    return width / views;
}
} // namespace bcamsync
