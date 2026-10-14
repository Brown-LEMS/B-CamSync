#include "bcamsync/session.hpp"
#include "session_test_access.hpp"
#include "bcamsync/phase.hpp"
#include <algorithm>
#include <arpa/inet.h>
#include <atomic>
#include <ctime>
#include <iostream>
#include <stdexcept>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>

using namespace bcamsync;
using namespace std::chrono_literals;
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
    require(rejected, "expected invalid session operation rejection");
}
struct Probe {
    std::atomic<bool> running{false}, fail_cancel{false}, fail_stop{false},
        empty_timing{false}, capture_failure{false}, pause_read{false}, read_entered{false};
    std::atomic<unsigned> starts{0}, stops{0}, snapshots{0}, snapshot_delay_ms{0};
    std::int64_t sensor_offset_ns = 0;
};
/// Software fixture generates complete timing history independently of frame reads.
class SyntheticBackend final : public AcquisitionBackend {
  public:
    SyntheticBackend(UnitConfig unit, std::shared_ptr<Probe> probe, unsigned fps = 40)
        : fps_(fps), unit_(std::move(unit)), probe_(std::move(probe)) {}
    void start(const std::string& session, std::uint64_t round) override {
        require(!session.empty() && round != 0, "backend must receive a synchronization identity");
        probe_->running = true;
        ++probe_->starts;
    }
    void stop() override {
        probe_->running = false;
        ++probe_->stops;
        if (probe_->fail_stop)
            throw std::runtime_error("injected backend stop failure");
    }
    TimingSnapshot snapshot() const override {
        require(probe_->running, "stopped backend cannot supply timing");
        ++probe_->snapshots;
        std::this_thread::sleep_for(std::chrono::milliseconds(probe_->snapshot_delay_ms.load()));
        return snapshot_at(timestamp());
    }
    TimingSnapshot snapshot_at(std::int64_t now) const {
        const auto period = 1000000000LL / fps_;
        const auto last = static_cast<std::uint64_t>(now / period);
        const auto count = std::min<std::uint64_t>(last, 240);
        TimingSnapshot result;
        result.unit_id = unit_.id;
        result.snapshot_timestamp_ns = now;
        if (probe_->empty_timing)
            return result;
        for (std::uint64_t i = 0; i < count; ++i) {
            FrameTiming f;
            f.camera_sequence = last - count + 1 + i;
            f.timestamp_ns = static_cast<std::int64_t>(f.camera_sequence) * period;
            // A later sensor epoch keeps early-boot history positive at nonzero offsets.
            f.sensor_timestamp_ns = f.timestamp_ns + probe_->sensor_offset_ns;
            f.requested_duration_us = f.reported_duration_us = period / 1000;
            result.frames.push_back(f);
        }
        return result;
    }
    CorrectionStatus adjust(const Pulse&) override {
        throw std::logic_error("aligned timing fixture needs no correction");
    }
    void cancel() override {
        if (probe_->fail_cancel)
            throw std::runtime_error("injected backend cancel failure");
    }
    std::shared_ptr<const FramePacket> read(std::chrono::milliseconds) override {
        if (!probe_->running)
            return {};
        auto packet = std::make_shared<FramePacket>();
        packet->unit_id = unit_.id;
        packet->timestamp_ns = timestamp();
        packet->sensor_timestamp_ns = packet->timestamp_ns + probe_->sensor_offset_ns;
        packet->sequence =
            static_cast<std::uint64_t>(packet->timestamp_ns / (1000000000LL / fps_));
        packet->width = unit_.fake.width;
        packet->height = unit_.fake.height;
        packet->views = 4;
        auto storage = std::make_shared<FrameStorage>();
        storage->pixels.assign(static_cast<std::size_t>(unit_.fake.width) * unit_.fake.height,
                               static_cast<std::uint8_t>(packet->sequence % 256));
        packet->storage = std::move(storage);
        if (probe_->pause_read) {
            probe_->read_entered = true;
            while (probe_->pause_read) std::this_thread::sleep_for(1ms);
        }
        return packet;
    }
    std::string fault() const override {
        return probe_->capture_failure ? "injected camera failure" : "";
    }

  private:
    std::int64_t timestamp() const {
        timespec value{};
        if (clock_gettime(CLOCK_BOOTTIME, &value) != 0)
            throw std::runtime_error("cannot read fixture capture clock");
        return static_cast<std::int64_t>(value.tv_sec) * 1000000000LL + value.tv_nsec;
    }
    unsigned fps_;
    UnitConfig unit_;
    std::shared_ptr<Probe> probe_;
};
std::uint16_t free_port() {
    static unsigned candidate = 59300;
    for (; candidate < 65535; ++candidate) {
        const auto fd = socket(AF_INET, SOCK_STREAM, 0);
        if (fd < 0)
            throw std::runtime_error("cannot create test socket");
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port = htons(static_cast<std::uint16_t>(candidate));
        inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
        const bool available =
            bind(fd, reinterpret_cast<const sockaddr*>(&addr), sizeof(addr)) == 0;
        close(fd);
        if (available)
            return static_cast<std::uint16_t>(candidate++);
    }
    throw std::runtime_error("no free test endpoint");
}
UnitConfig unit(const std::string& id, const std::string& node) {
    UnitConfig result;
    result.id = id;
    result.node_id = node;
    result.camera_id = "camera-" + id;
    result.fake.width = 16;
    result.fake.height = 8;
    return result;
}
Config configuration(bool network) {
    Config config;
    config.backend = "fake";
    config.clock_mode = "host";
    config.group_id = "software-session-fixture";
    config.reference_unit = "reference";
    config.nodes = {{"a", "127.0.0.1", "", free_port()}};
    if (network)
        config.nodes.push_back({"b", "127.0.0.1", "", free_port()});
    config.units = {unit("reference", "a"), unit("follower", network ? "b" : "a")};
    config.request_timeout_seconds = 2;
    config.startup_timeout_seconds = 12;
    config.validate();
    return config;
}
std::vector<std::unique_ptr<AcquisitionBackend>>
backends(const Config& config, const std::string& node,
         const std::vector<std::shared_ptr<Probe>>& probes) {
    std::vector<std::unique_ptr<AcquisitionBackend>> result;
    for (const auto& u : config.units)
        if (u.node_id == node) {
            result.push_back(std::make_unique<SyntheticBackend>(u, probes.at(result.size()), config.fps));
        }
    return result;
}
void local_session() {
    std::cout << "checking single-node multi-board runtime\n" << std::flush;
    auto config = configuration(false);
    config.reference_unit = "follower";
    auto p0 = std::make_shared<Probe>(), p1 = std::make_shared<Probe>();
    p0->sensor_offset_ns = 1000000000;
    p1->sensor_offset_ns = 2000000000;
    auto session = detail::SessionTestAccess::create(config, "a", backends(config, "a", {p0, p1}));
    session.start();
    require(session.wait_ready(8s), "single-node boards must converge without image consumption");
    require(p0->snapshots > 1 && p1->snapshots > 1, "runtime continually observes all boards");
    const auto status = session.status();
    require(status.ready && status.fault.empty() && status.units.size() == 2,
            "group reports both local units ready");
    for (const auto& state : status.units)
        require(state.ready && state.fault.empty(), "unit readiness mirrors synchronization");
    for (const auto& state : status.units)
        if (state.unit_id == config.reference_unit)
            require(state.state == "reference",
                    "reference selection is independent of unit enumeration");
    for (const auto& state : status.units)
        if (state.unit_id != config.reference_unit)
            require(state.measurement_end_ns > 0,
                    "settled metrics identify their observation window");
    auto held = session.read("reference", 100ms);
    auto other = session.read("follower", 100ms);
    require(held && other && held->unit_id == "reference" && other->unit_id == "follower",
            "read routes packets to requested board");
    require(held->clock_valid && held->sensor_timestamp_ns == held->timestamp_ns + p0->sensor_offset_ns &&
                other->clock_valid && other->sensor_timestamp_ns == other->timestamp_ns + p1->sensor_offset_ns,
            "runtime maps backend local timestamps while preserving original SDK sensor readings");
    require(held->views == 4 && held->view_width() == 4, "board packets expose four camera views");
    const auto value = held->view_data(0)[0];
    const auto sequence = held->sequence;
    std::this_thread::sleep_for(40ms);
    const auto fresh = session.read("reference", 100ms);
    require(fresh && fresh->sequence > sequence && held->view_data(0)[0] == value,
            "continued acquisition retains older frame contents");
    rejects([&] { session.read("missing", 10ms); });
    session.stop();
    session.stop();
    require(!p0->running && !p1->running && !session.status().ready,
            "stop releases both backends and readiness");
    require(held->view_data(3)[16] == value, "delivered frame storage survives session stop");
}
void network_session() {
    std::cout << "checking two-node loopback coordination\n" << std::flush;
    const auto config = configuration(true);
    auto a = std::make_shared<Probe>(), b = std::make_shared<Probe>();
    auto follower_config = config;
    std::reverse(follower_config.nodes.begin(), follower_config.nodes.end());
    std::reverse(follower_config.units.begin(), follower_config.units.end());
    follower_config.units.front().exposure_us = 2000;
    follower_config.units.front().queue_capacity = 8;
    auto follower = detail::SessionTestAccess::create(follower_config, "b", backends(follower_config, "b", {b}));
    auto coordinator = detail::SessionTestAccess::create(config, "a", backends(config, "a", {a}));
    follower.start();
    coordinator.start();
    const auto ready = coordinator.wait_ready(8s);
    if (!ready) {
        for (const auto* session : {&coordinator, &follower}) {
            const auto state = session->status();
            std::cerr << "session state=" << state.state << " ready=" << state.ready
                      << " fault=" << state.fault << '\n';
        }
    }
    require(ready, "coordinator must align remote unit");
    require(follower.wait_ready(2s), "readiness propagates to follower session");
    const auto mapped = coordinator.read("reference", 100ms);
    require(mapped && mapped->clock_valid && mapped->timestamp_ns > mapped->sensor_timestamp_ns,
            "runtime maps cross-node frame timestamps without changing sensor time");
    require(mapped->sequence == static_cast<std::uint64_t>(mapped->sensor_timestamp_ns / (1000000000LL / config.fps)),
            "common-clock delivery preserves the original sensor capture identity");
    const auto retained_sensor = mapped->sensor_timestamp_ns, retained_common = mapped->timestamp_ns;
    const auto retained_pixel = mapped->view_data(0)[0];
    for (const auto& state : follower.status().units)
        if (state.unit_id != config.reference_unit)
            require(state.measurement_end_ns > 0,
                    "measurement identity crosses node status channel");
    require(coordinator.read("reference", 100ms) && follower.read("follower", 100ms),
            "each node consumes its own board frames");
    rejects([&] { coordinator.read("follower", 10ms); });
    rejects([&] { follower.read("reference", 10ms); });
    b->snapshot_delay_ms = 100;
    const auto previous_snapshots = b->snapshots.load();
    const auto entering_request = std::chrono::steady_clock::now() + 2s;
    while (b->snapshots <= previous_snapshots &&
           std::chrono::steady_clock::now() < entering_request)
        std::this_thread::sleep_for(1ms);
    require(b->snapshots > previous_snapshots, "fixture entered in-progress remote observation");
    coordinator.stop();
    const auto remote_stopped = std::chrono::steady_clock::now() + 1s;
    while (follower.status().state != "stopped" &&
           std::chrono::steady_clock::now() < remote_stopped)
        std::this_thread::sleep_for(1ms);
    require(follower.status().state == "stopped" && !follower.status().ready &&
                follower.status().fault.empty(),
            "graceful coordinator shutdown stops follower without a fault");
    require(!a->running && !b->running, "group shutdown releases both acquisition backends");
    follower.stop();
    b->snapshot_delay_ms = 0;
    follower.start();
    coordinator.start();
    require(coordinator.wait_ready(8s) && follower.wait_ready(2s),
            "stopped sessions restart with a new group session");
    require(a->starts == 2 && b->starts == 2, "restart initializes both local backends");
    require(mapped->sensor_timestamp_ns == retained_sensor && mapped->timestamp_ns == retained_common &&
                mapped->view_data(0)[0] == retained_pixel,
            "retained sensor time, mapped metadata, and pixels survive restart");
    follower.stop();
    const auto deadline = std::chrono::steady_clock::now() + 3s;
    GroupStatus state;
    do {
        state = coordinator.status();
        if (!state.fault.empty())
            break;
        std::this_thread::sleep_for(20ms);
    } while (std::chrono::steady_clock::now() < deadline);
    require(!state.ready && !state.fault.empty(), "peer loss faults synchronized group");
    require(a->running && coordinator.read("reference", 100ms),
            "peer loss leaves healthy local acquisition readable");
    coordinator.stop();
}
void incompatible_peer() {
    std::cout << "checking topology handshake rejection\n" << std::flush;
    auto config = configuration(true);
    auto mismatch = config;
    mismatch.group_id = "different-group";
    auto a = std::make_shared<Probe>(), b = std::make_shared<Probe>();
    auto follower = detail::SessionTestAccess::create(mismatch, "b", backends(mismatch, "b", {b}));
    auto coordinator = detail::SessionTestAccess::create(config, "a", backends(config, "a", {a}));
    follower.start();
    coordinator.start();
    rejects([&] { coordinator.wait_ready(2s); });
    require(!coordinator.status().fault.empty(), "handshake mismatch is reported");
    coordinator.stop();
    follower.stop();
}
void malformed_sessions() {
    const auto config = configuration(false);
    rejects([&] { auto invalid = detail::SessionTestAccess::create(config, "a", {}); });
    auto p0 = std::make_shared<Probe>(), p1 = std::make_shared<Probe>();
    rejects([&] { auto invalid = detail::SessionTestAccess::create(config, "missing", backends(config, "a", {p0, p1})); });
}
void local_capture_failures() {
    auto config = configuration(false);
    auto healthy = std::make_shared<Probe>(), failed = std::make_shared<Probe>();
    failed->capture_failure = true;
    auto session = detail::SessionTestAccess::create(config, "a", backends(config, "a", {healthy, failed}));
    session.start();
    rejects([&] { session.wait_ready(2s); });
    require(session.read("reference", 100ms) != nullptr, "a camera fault leaves other units readable");
    rejects([&] { session.read("follower", 100ms); });
    failed->capture_failure = false;
    session.stop();
}
void cleanup_failures() {
    std::cout << "checking backend cleanup failures\n" << std::flush;
    for (const bool cancel_failure : {true, false}) {
        auto config = configuration(false);
        config.units.resize(1);
        auto probe = std::make_shared<Probe>();
        auto session = detail::SessionTestAccess::create(config, "a", backends(config, "a", {probe}));
        session.start();
        require(session.wait_ready(1s), "single-reference fixture becomes ready");
        if (cancel_failure)
            probe->fail_cancel = true;
        else
            probe->fail_stop = true;
        session.stop();
        require(!probe->running && probe->stops > 0,
                "stop is attempted even after cancellation fails");
        const auto status = session.status();
        require(status.state == "fault" && !status.ready &&
                    status.fault.find("injected backend") != std::string::npos,
                "backend cleanup failure is reported after explicit shutdown");
        for (const auto& state : status.units)
            require(!state.ready, "cleanup failure clears unit readiness");
    }
}
void startup_deadline() {
    std::cout << "checking startup timing deadline\n" << std::flush;
    auto config = configuration(false);
    config.units.resize(1);
    config.startup_timeout_seconds = .2;
    auto probe = std::make_shared<Probe>();
    probe->empty_timing = true;
    auto session = detail::SessionTestAccess::create(config, "a", backends(config, "a", {probe}));
    session.start();
    const auto acquisition_deadline = std::chrono::steady_clock::now() + 1s;
    while (!probe->running && std::chrono::steady_clock::now() < acquisition_deadline)
        std::this_thread::sleep_for(1ms);
    require(session.read("reference", 100ms) && !session.status().ready,
            "frames are available before synchronization readiness");
    rejects([&] { session.wait_ready(2s); });
    require(session.status().fault.find("startup") != std::string::npos,
            "missing startup timing reaches configured deadline");
    require(probe->running && session.read("reference", 100ms),
            "startup synchronization fault leaves frames available");
    session.stop();
    require(!probe->running, "explicit stop releases the acquisition backend");
}
void retained_read_during_address_refresh() {
    auto config = configuration(true);
    auto probe = std::make_shared<Probe>();
    auto session = detail::SessionTestAccess::create(config, "a", backends(config, "a", {probe}));
    session.start();
    rejects([&] { session.set_node_addresses({{"a", "127.0.0.1"}, {"b", "127.0.0.2"}}); });
    const auto deadline = std::chrono::steady_clock::now() + 2s;
    while (!probe->running && std::chrono::steady_clock::now() < deadline)
        std::this_thread::sleep_for(1ms);
    require(probe->running, "backend starts before pending group coordination");
    probe->pause_read = true;
    std::shared_ptr<const FramePacket> delivered;
    std::thread consumer([&] { delivered = session.read("reference", 1ms); });
    while (!probe->read_entered && std::chrono::steady_clock::now() < deadline)
        std::this_thread::sleep_for(1ms);
    if (!probe->read_entered) {
        probe->pause_read = false;
        consumer.join();
        throw std::runtime_error("consumer did not borrow a packet");
    }
    session.stop();
    rejects([&] { session.set_node_addresses({{"a", "127.0.0.1"}}); });
    rejects([&] { session.set_node_addresses({{"a", "invalid"}, {"b", "127.0.0.2"}}); });
    probe->pause_read = false;
    for (unsigned i = 0; i < 2000; ++i)
        session.set_node_addresses({{"a", "127.0.0.1"}, {"b", "127.0.0.2"}});
    consumer.join();
    require(delivered && delivered->sensor_timestamp_ns > 0 && !delivered->clock_valid,
            "in-flight delivery remains readable while stopped network endpoints change");
    require(probe->starts == 1 && probe->stops == 1,
            "endpoint refresh retains camera ownership without starting capture");
}
void cached_clock_expiry() {
    auto config = configuration(true);
    config.clock_max_age_seconds = .05;
    auto probe = std::make_shared<Probe>();
    auto session = detail::SessionTestAccess::create(config, "a", backends(config, "a", {probe}));
    session.start();
    const auto deadline = std::chrono::steady_clock::now() + 1s;
    std::shared_ptr<const FramePacket> frame;
    do {
        frame = session.read("reference", 1ms);
        if (frame && frame->clock_valid) break;
        std::this_thread::sleep_for(1ms);
    } while (std::chrono::steady_clock::now() < deadline);
    require(frame && frame->clock_valid, "initial clock mapping is available during connection setup");
    std::this_thread::sleep_for(100ms);
    const auto expired = session.read("reference", 1ms);
    require(expired && !expired->clock_valid && expired->timestamp_ns == 0,
            "blocking node setup cannot keep a stale mapping marked valid");
    require(frame->clock_valid, "mapping updates leave previously delivered metadata immutable");
    session.stop();
}
void acquisition_without_common_clock() {
#ifdef __linux__
    auto config = configuration(true);
    config.backend = "arducam";
    config.clock_mode = "network";
    config.network_mode = "existing";
    config.nodes[1].address = "127.0.0.2";
    config.startup_timeout_seconds = .2;
    auto probe = std::make_shared<Probe>();
    auto session = detail::SessionTestAccess::create(config, "b", backends(config, "b", {probe}));
    session.start();
    rejects([&] { session.wait_ready(2s); });
    const auto frame = session.read("follower", 100ms);
    require(frame && !frame->clock_valid && frame->timestamp_ns == 0 && frame->sensor_timestamp_ns > 0,
            "clock startup failure preserves local frames with explicit unavailable group time");
    require(probe->running, "backend injection does not disable physical clock policy");
    session.stop();
#endif
}
void early_boot_history() {
    auto config = configuration(false);
    auto probe = std::make_shared<Probe>();
    SyntheticBackend backend(config.units.front(), probe);
    backend.start("early-boot", 1);
    const auto raw = backend.read(100ms);
    const auto observation = backend.snapshot();
    require(raw && raw->timestamp_ns == raw->sensor_timestamp_ns &&
                raw->sensor_timestamp_ns <= observation.snapshot_timestamp_ns,
            "acquisition fixture exposes node-local source timestamps before runtime mapping");
    for (const auto offset : {0LL, 1000000000LL, 2000000000LL}) {
        probe->sensor_offset_ns = offset;
        for (const auto now : {25000000LL, 1000000000LL, 2000000000LL, 5975000000LL, 6000000000LL,
                               8000000000LL, 10000000000LL})
            validate_timing(backend.snapshot_at(now), now);
    }
    bool pending = false;
    try { validate_timing(backend.snapshot_at(1), 1); }
    catch (const WindowPending&) { pending = true; }
    require(pending, "Before the first frame, early-boot timing is pending");
    backend.stop();
}
} // namespace
int main() {
    try {
        early_boot_history();
        malformed_sessions();
        local_session();
        network_session();
        incompatible_peer();
        cleanup_failures();
        local_capture_failures();
        startup_deadline();
        acquisition_without_common_clock();
        cached_clock_expiry();
        retained_read_during_address_refresh();
        std::cout << "session tests passed\n";
    } catch (const std::exception& e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
