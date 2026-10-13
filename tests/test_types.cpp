#include "bcamsync/types.hpp"
#include "wire.hpp"
#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <unistd.h>

using namespace bcamsync;
namespace {
void require(bool condition, const char* message) {
    if (!condition)
        throw std::runtime_error(message);
}
template <class F> void rejects(F action) {
    bool rejected = false;
    try {
        action();
    } catch (const std::exception&) {
        rejected = true;
    }
    require(rejected, "expected malformed input rejection");
}
Config valid() {
    Config c;
    c.group_id = "laboratory";
    c.reference_unit = "board-a";
    c.network_mode = "existing";
    c.nodes = {{"node-a", "127.0.0.1", "eth0", 59101}, {"node-b", "127.0.0.2", "eth0", 59102}};
    UnitConfig a;
    a.id = "board-a";
    a.node_id = "node-a";
    a.camera_id = "camera-a";
    auto b = a;
    b.id = "board-b";
    b.node_id = "node-b";
    b.camera_id = "camera-b";
    c.units = {a, b};
    return c;
}
void topology() {
    auto c = valid();
    c.validate();
    require(c.reference_node() == "node-a", "reference unit selects coordinator node");
    c.reference_unit = "board-b";
    c.validate();
    require(c.reference_node() == "node-b", "reference unit may be selected on either node");
    rejects([&] { c.node("absent"); });
    rejects([&] { c.unit("absent"); });
    const auto invalid = [](auto mutate) {
        auto config = valid();
        mutate(config);
        rejects([&] { config.validate(); });
    };
    invalid([](Config& x) { x.reference_unit = "absent"; });
    invalid([](Config& x) { x.group_id.clear(); });
    invalid([](Config& x) { x.nodes[1].id = x.nodes[0].id; });
    invalid([](Config& x) {
        x.nodes[1].address = x.nodes[0].address;
        x.nodes[1].port = x.nodes[0].port;
    });
    invalid([](Config& x) { x.nodes[0].address = "invalid"; });
    invalid([](Config& x) { x.nodes[0].port = 0; });
    invalid([](Config& x) { x.nodes.push_back({"empty", "127.0.0.3", "eth0", 59103}); });
    invalid([](Config& x) { x.units[1].id = x.units[0].id; });
    invalid([](Config& x) { x.units[1].node_id = "absent"; });
    invalid([](Config& x) {
        x.units[1].node_id = x.units[0].node_id;
        x.units[1].camera_id = x.units[0].camera_id;
    });
    invalid([](Config& x) { x.fps = 0; });
    invalid([](Config& x) { x.fps = 1000001; });
    invalid([](Config& x) { x.units[0].pool_capacity = x.units[0].queue_capacity; });
    invalid([](Config& x) { x.units[0].queue_capacity = std::numeric_limits<unsigned>::max(); });
    invalid([](Config& x) { x.network_mode = "unknown"; });
    invalid([](Config& x) { x.clock_port = 0; });
    invalid([](Config& x) { x.network_mode = "auto"; x.nodes[0].interface.clear(); });
    invalid([](Config& x) { x.backend = "fake"; x.units[0].fake.width = 15; });
    invalid([](Config& x) { x.backend = "fake"; x.units[0].fake.height = 0; });
    invalid([](Config& x) {
        x.backend = "fake";
        x.units[0].fake.width = std::numeric_limits<unsigned>::max() - 3;
        x.units[0].fake.height = std::numeric_limits<unsigned>::max();
    });
    invalid([](Config& x) { x.units[0].profile = "unknown"; });
    invalid([](Config& x) { x.units[0].analogue_gain = std::numeric_limits<float>::quiet_NaN(); });
    invalid([](Config& x) { x.units[0].exposure_us = 25000; });
    invalid([](Config& x) { x.request_timeout_seconds = std::numeric_limits<double>::infinity(); });
    invalid([](Config& x) { x.startup_timeout_seconds = -1; });
    invalid([](Config& x) { x.startup_timeout_seconds = 1e10; });
    invalid([](Config& x) { x.request_timeout_seconds = 4e9; });
}
void existing_address_network() {
    auto config = valid();
    config.network_mode = "existing";
    for (auto& node : config.nodes) node.interface.clear();
    config.validate();
}
void shared_contract() {
    const auto c = valid();
    auto local = c;
    local.units[0].camera_id = "other sensor";
    local.units[0].exposure_us = 5000;
    local.units[0].analogue_gain = 2;
    local.units[0].auto_gain = false;
    local.units[0].queue_capacity = 100;
    local.units[0].pool_capacity = 101;
    local.units[1].profile = "arducam_b0267_pi5";
    local.nodes[0].address = "192.0.2.10";
    local.nodes[0].interface = "enp1s0";
    local.nodes[0].port = 12345;
    local.clock_port = 12346;
    local.clock_max_offset_ms = 0.5;
    local.request_timeout_seconds = 20;
    local.startup_timeout_seconds = 1200;
    std::reverse(local.nodes.begin(), local.nodes.end());
    std::reverse(local.units.begin(), local.units.end());
    require(c.sync_json() == local.sync_json(), "shared contract ignores host, acquisition, and order differences");
    require(c.to_json() != local.to_json(), "resolved acquisition configuration retains local differences");
    auto changed = c;
    changed.group_id = "another group";
    require(c.sync_json() != changed.sync_json(), "group identity is shared");
    changed = c;
    changed.reference_unit = "board-b";
    require(c.sync_json() != changed.sync_json(), "chosen reference is shared");
    changed = c;
    changed.fps = 50;
    require(c.sync_json() != changed.sync_json(), "group cadence is shared");
    changed = c;
    changed.units[1].node_id = "node-a";
    changed.nodes.pop_back();
    require(c.sync_json() != changed.sync_json(), "membership and clock domain are shared");
}
void configuration_modes() {
    auto c = valid();
    c.network_mode = "auto";
    for (auto& n : c.nodes) n.address.clear();
    c.validate();
    c.network_mode = "existing";
    rejects([&] { c.validate(); });
    c.backend = "fake";
    c.network_mode = "auto";
    rejects([&] { c.validate(); });
    c = valid();
    c.units.pop_back();
    c.nodes.pop_back();
    c.nodes[0].address.clear();
    c.nodes[0].interface.clear();
    c.group_id = "group / display name";
    c.nodes[0].id = c.units[0].node_id = "node display name";
    c.units[0].id = c.reference_unit = "board display name";
    c.units[0].queue_capacity = 100;
    c.units[0].pool_capacity = 101;
    c.clock_max_offset_ms = 1;
    c.request_timeout_seconds = 20;
    c.startup_timeout_seconds = 1200;
    c.fps = 80;
    c.validate();
    require(c.sync_json().find("local") != std::string::npos, "single node needs no network endpoint");
    c.units[0].pool_capacity = c.units[0].queue_capacity;
    rejects([&] { c.validate(); });
    c = valid();
    c.nodes.clear();
    c.units.clear();
    for (unsigned i = 0; i < 70; ++i) {
        const auto suffix = std::to_string(i);
        c.nodes.push_back({"node " + suffix, "", "eth0", 58080});
        UnitConfig u;
        u.id = "board " + suffix;
        u.node_id = c.nodes.back().id;
        u.camera_id = "camera";
        c.units.push_back(u);
    }
    c.reference_unit = c.units.front().id;
    c.network_mode = "auto";
    c.validate();
    require(c.nodes.size() == 70 && c.units.size() == 70, "topology has no arbitrary board or node ceiling");
}
void packet_views() {
    auto storage = std::make_shared<FrameStorage>();
    storage->pixels.resize(128);
    for (std::size_t i = 0; i < storage->pixels.size(); ++i)
        storage->pixels[i] = static_cast<std::uint8_t>(i);
    FramePacket packet;
    packet.width = 16;
    packet.height = 8;
    packet.views = 4;
    packet.storage = storage;
    require(packet.view_width() == 4, "four equal camera views");
    require(packet.view_data(0)[0] == 0 && packet.view_data(3)[0] == 12,
            "view columns reference board storage");
    require(packet.view_data(1)[packet.width] == 20, "camera view rows retain board stride");
    storage.reset();
    require(packet.view_data(2)[16] == 24, "frame packet retains shared storage");
    rejects([&] { packet.view_data(4); });
    auto bad = packet;
    bad.width = 15;
    rejects([&] { bad.view_data(0); });
    bad = packet;
    bad.storage.reset();
    rejects([&] { bad.view_data(0); });
    bad = packet;
    bad.views = 0;
    rejects([&] { bad.view_data(0); });
    bad = packet;
    bad.height = 7;
    rejects([&] { bad.view_data(0); });
}
struct TemporaryConfig {
    std::string path;
    TemporaryConfig() {
        char name[] = "/tmp/bcamsync-config-XXXXXX";
        const auto fd = mkstemp(name);
        if (fd < 0)
            throw std::runtime_error("cannot create config fixture");
        close(fd);
        path = name;
    }
    ~TemporaryConfig() { unlink(path.c_str()); }
    void write(const std::string& text) const {
        std::ofstream file(path);
        file << text;
        if (!file)
            throw std::runtime_error("cannot write config fixture");
    }
};
void wire_membership() {
    GroupStatus status;
    for (unsigned i = 0; i < 70; ++i) {
        UnitStatus unit;
        unit.unit_id = "board-" + std::to_string(i);
        status.units.push_back(unit);
    }
    require(detail::status_from_json(detail::status_json(status)).units.size() == 70,
            "wire status preserves configured groups beyond the old unit ceiling");
    auto malformed = detail::status_json(status);
    malformed["units"] = 70;
    rejects([&] { detail::status_from_json(malformed); });
}
void configuration_files() {
    TemporaryConfig file;
    const std::string text =
        R"({"group_id":"example","reference_unit":"board","nodes":[{"id":"node","address":"127.0.0.1","port":59100}],"units":[{"id":"board","node_id":"node","camera_id":"camera"}]})";
    file.write(text);
    const auto c = Config::from_file(file.path);
    require(c.unit("board").camera_id == "camera" && c.nodes[0].port == 59100,
            "configuration loads device and endpoint fields");
    require(c.fps == 40 && c.network_mode == "auto" && c.clock_port == 58081, "configuration defaults");
    require(c.to_json().find("\"width\"") == std::string::npos, "physical geometry stays in the profile");
    require(c.unit("board").profile == "arducam_b0267_pi4" && c.unit("board").auto_gain,
            "default board profile selects startup gain calibration");
    auto manual = c;
    manual.units[0].auto_gain = false;
    manual.units[0].analogue_gain = 2.0F;
    file.write(manual.to_json());
    const auto restored = Config::from_file(file.path);
    require(!restored.units[0].auto_gain && restored.units[0].analogue_gain == 2.0F,
            "manual gain policy survives resolved configuration exchange");
    require(restored.to_json() == manual.to_json(), "resolved configuration round trip is complete");
    auto fake = c;
    fake.backend = "fake";
    fake.fps = 80;
    fake.network_mode = "existing";
    fake.clock_port = 12346;
    fake.units[0].fake = {32, 8, 1234, 25};
    file.write(fake.to_json());
    const auto restored_fake = Config::from_file(file.path);
    require(restored_fake.fps == 80 && restored_fake.network_mode == "existing" &&
                restored_fake.clock_port == 12346 && restored_fake.units[0].fake.width == 32 &&
                restored_fake.units[0].fake.height == 8 && restored_fake.units[0].fake.initial_phase_us == 1234 &&
                restored_fake.units[0].fake.drift_ppm == 25,
            "group cadence, network mode, and nested synthetic settings round trip");
    auto pi5 = c;
    pi5.units[0].profile = "arducam_b0267_pi5";
    file.write(pi5.to_json());
    const auto restored_pi5 = Config::from_file(file.path);
    require(restored_pi5.units[0].profile == "arducam_b0267_pi5" && restored_pi5.units[0].auto_gain,
            "Pi5 profile preserves startup gain policy through configuration exchange");
    auto mixed_nodes = valid();
    mixed_nodes.units[1].profile = "arducam_b0267_pi5";
    mixed_nodes.validate();
    file.write(mixed_nodes.to_json());
    require(Config::from_file(file.path).unit("board-b").profile == "arducam_b0267_pi5",
            "One synchronization group may contain Pi4 and Pi5 nodes");
    auto legacy = text;
    legacy.insert(legacy.find("\"camera_id\""), "\"raw_format\":\"R8\",");
    file.write(legacy);
    rejects([&] { Config::from_file(file.path); });
    legacy = text;
    legacy.insert(legacy.find("\"camera_id\""), "\"width\":5120,");
    file.write(legacy);
    rejects([&] { Config::from_file(file.path); });
    auto negative = text;
    negative.insert(negative.find("\"units\""), "\"fps\":-1,");
    file.write(negative);
    rejects([&] { Config::from_file(file.path); });
    file.write("{malformed");
    rejects([&] { Config::from_file(file.path); });
    file.write("{}");
    rejects([&] { Config::from_file(file.path); });
    auto bad = text;
    const auto position = bad.find("59100");
    bad.replace(position, 5, "70000");
    file.write(bad);
    rejects([&] { Config::from_file(file.path); });
    rejects([&] { Config::from_file(file.path + "/absent"); });
}
} // namespace
int main() {
    try {
        topology();
        shared_contract();
        existing_address_network();
        configuration_modes();
        packet_views();
        configuration_files();
        wire_membership();
        std::cout << "types tests passed\n";
    } catch (const std::exception& e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
