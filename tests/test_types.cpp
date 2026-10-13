#include "bcamsync/types.hpp"
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
    c.nodes = {{"node-a", "127.0.0.1", "eth0", 59101}, {"node-b", "127.0.0.2", "eth0", 59102}};
    UnitConfig a;
    a.id = "board-a";
    a.node_id = "node-a";
    a.camera_id = "camera-a";
    a.width = 16;
    a.height = 8;
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
    invalid([](Config& x) { x.units[0].width = 15; });
    invalid([](Config& x) { x.units[0].height = 0; });
    invalid([](Config& x) { x.units[0].views = 0; });
    invalid([](Config& x) { x.units[0].fps = 0; });
    invalid([](Config& x) { x.units[1].fps = 30; });
    invalid([](Config& x) { x.units[0].pool_capacity = x.units[0].queue_capacity + 1; });
    invalid([](Config& x) { x.units[0].profile = "unknown"; });
    invalid([](Config& x) { x.units[0].analogue_gain = std::numeric_limits<float>::quiet_NaN(); });
    invalid([](Config& x) { x.units[0].exposure_us = 25000; });
    invalid([](Config& x) { x.request_timeout_seconds = std::numeric_limits<double>::infinity(); });
    invalid([](Config& x) { x.startup_timeout_seconds = -1; });
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
void configuration_files() {
    TemporaryConfig file;
    const std::string text =
        R"({"group_id":"example","reference_unit":"board","nodes":[{"id":"node","address":"127.0.0.1","port":59100}],"units":[{"id":"board","node_id":"node","camera_id":"camera","width":16,"height":8}]})";
    file.write(text);
    const auto c = Config::from_file(file.path);
    require(c.unit("board").width == 16 && c.nodes[0].port == 59100,
            "configuration loads capture and endpoint fields");
    require(c.unit("board").views == 4 && c.unit("board").fps == 40, "configuration defaults");
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
    auto legacy = text;
    legacy.insert(legacy.find("\"width\""), "\"raw_format\":\"R8\",");
    file.write(legacy);
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
        packet_views();
        configuration_files();
        std::cout << "types tests passed\n";
    } catch (const std::exception& e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
