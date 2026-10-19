#include "bcamsync/sync.hpp"
#include "bcamsync/actuator.hpp"
#include "bcamsync/session.hpp"
#include "wire.hpp"
#include <algorithm>
#include <arpa/inet.h>
#include <atomic>
#include <condition_variable>
#include <deque>
#include <iostream>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>

using namespace bcamsync;
using namespace std::chrono_literals;
namespace {
void require(bool value, const char* message) { if (!value) throw std::runtime_error(message); }
template<class F> void rejects(F action) {
    bool rejected = false;
    try { action(); } catch (const std::exception&) { rejected = true; }
    require(rejected, "invalid synchronization operation must be rejected");
}
template<class F> void await(F predicate, std::chrono::milliseconds duration, const char* message) {
    const auto end = std::chrono::steady_clock::now() + duration;
    while (!predicate()) {
        if (std::chrono::steady_clock::now() >= end) throw std::runtime_error(message);
        std::this_thread::sleep_for(1ms);
    }
}
std::uint16_t free_port() {
    const int fd = socket(AF_INET, SOCK_STREAM, 0);
    require(fd >= 0, "test endpoint creation");
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    require(bind(fd, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0, "test endpoint binding");
    socklen_t size = sizeof(address);
    require(getsockname(fd, reinterpret_cast<sockaddr*>(&address), &size) == 0, "test endpoint lookup");
    close(fd);
    return ntohs(address.sin_port);
}
CorrectionStatus status(const PhaseActuator::State& state) {
    return {state.state, state.correction_id, state.restored_timestamp_ns};
}
/// Application-owned capture loop. Synchronization never starts, stops, or reads this producer.
class Capture final : public SyncUnit {
public:
    std::string id;
    std::atomic<bool> running{false}, hold_restoration{false}, wrong_ack{false}, fail_snapshot{false}, fail_bind{false}, idle_feedback{false}, capture_fault{false};
    std::atomic<unsigned> starts{0}, binds{0}, cancels{0};
    mutable std::atomic<unsigned> snapshots{0};
    std::atomic<std::uint64_t> sequence{0};
    int control_margin_us = 1000;
    std::atomic<int> largest_delta_us{0};
    std::int64_t mapping_span_ns = 0;
    explicit Capture(std::string name, std::int64_t phase = 0) : id(std::move(name)), phase_(phase) {}
    ~Capture() override { stop_capture(); }
    void start_capture() {
        ++starts;
        running = true;
        worker_ = std::thread([this] {
            constexpr std::int64_t baseline = 25000;
            auto next = boot_time_ns();
            next = next - next % (baseline * 1000) + baseline * 1000 + phase_;
            PhaseActuator::Submission submitted{baseline, 0, false, 0};
            while (running) {
                std::unique_lock<std::mutex> lock(mutex_);
                changed_.wait_for(lock, std::chrono::nanoseconds(std::max<std::int64_t>(0, next - boot_time_ns())),
                                  [&] { return !running.load(); });
                if (!running) break;
                const auto seq = ++sequence;
                FrameTiming frame;
                frame.camera_sequence = seq;
                frame.timestamp_ns = frame.sensor_timestamp_ns = next;
                frame.requested_duration_us = frame.reported_duration_us = submitted.duration_us;
                frame.correction_id = submitted.correction_id;
                frames_.push_back(frame);
                while (frames_.size() > 240) frames_.pop_front();
                if (submitted.restoration && hold_restoration) pending_restoration_ = submitted;
                else actuator_->completed(submitted, seq, next);
                if (pending_restoration_ && !hold_restoration) {
                    actuator_->completed(*pending_restoration_, seq, next);
                    pending_restoration_.reset();
                }
                submitted = actuator_->next(baseline, monotonic_ns());
                next += submitted.duration_us * 1000;
            }
        });
    }
    void stop_capture() {
        running = false;
        changed_.notify_all();
        if (worker_.joinable()) worker_.join();
    }
    void bind(const std::string& session, std::uint64_t round) override {
        if (fail_bind) throw std::runtime_error("external bind failure");
        std::lock_guard<std::mutex> lock(mutex_);
        const auto state = actuator_->snapshot();
        if (!state.session_id.empty() && state.session_id != session) {
            if (state.state == "submitting" || state.state == "settling")
                throw std::runtime_error("previous control round is still active");
            actuator_ = std::make_unique<PhaseActuator>();
        }
        actuator_->bind(session, round);
        session_ = session;
        round_ = round;
        ++binds;
    }
    TimingSnapshot snapshot() const override {
        ++snapshots;
        if (fail_snapshot) throw std::runtime_error("external snapshot failure");
        std::lock_guard<std::mutex> lock(mutex_);
        TimingSnapshot result;
        result.unit_id = id;
        result.snapshot_timestamp_ns = boot_time_ns();
        result.frames.assign(frames_.begin(), frames_.end());
        for (auto& frame : result.frames) frame.mapping_span_ns = mapping_span_ns;
        result.correction = status(actuator_->snapshot());
        if (idle_feedback && result.correction.correction_id) {
            result.correction.state = "idle";
            result.correction.restored_timestamp_ns = 0;
        }
        return result;
    }
    CorrectionStatus adjust(const Pulse& pulse) override {
        std::lock_guard<std::mutex> lock(mutex_);
        largest_delta_us = std::max(largest_delta_us.load(), std::abs(pulse.delta_us));
        auto result = status(actuator_->adjust(session_, round_, pulse.correction_id, pulse.delta_us,
            pulse.count, pulse.expires_ns, boot_time_ns(), monotonic_ns(), 25000,
            25000 - control_margin_us, 25000 + control_margin_us));
        if (wrong_ack) ++result.correction_id;
        return result;
    }
    void cancel() override {
        std::lock_guard<std::mutex> lock(mutex_);
        ++cancels;
        actuator_->cancel(session_, round_);
    }
    std::string fault() const override { return capture_fault ? "external capture fault" : ""; }
    std::string binding_identity() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return session_;
    }
    CorrectionStatus correction() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return status(actuator_->snapshot());
    }
private:
    std::int64_t phase_;
    mutable std::mutex mutex_;
    std::condition_variable changed_;
    std::deque<FrameTiming> frames_;
    std::unique_ptr<PhaseActuator> actuator_ = std::make_unique<PhaseActuator>();
    std::optional<PhaseActuator::Submission> pending_restoration_;
    std::thread worker_;
    std::string session_;
    std::uint64_t round_ = 0;
};
SyncConfig group() {
    SyncConfig config;
    config.group_id = "external-capture";
    config.reference_unit = "reference";
    config.nodes = {{"a", "", "", free_port()}};
    config.units = {{"reference", "a"}, {"follower", "a"}};
    config.startup_timeout_seconds = 30;
    return config;
}
void application_ownership() {
    auto config = group();
    auto reference = std::make_shared<Capture>("reference");
    auto follower = std::make_shared<Capture>("follower", 2000000);
    reference->start_capture(); follower->start_capture();
    SyncSession session(config, "a", {{"follower", follower}, {"reference", reference}});
    require(session.status().state == "stopped", "construction leaves capture unchanged");
    session.start();
    rejects([&] { session.start(); });
    rejects([&] { session.set_node_addresses({{"a", "127.0.0.1"}}); });
    require(session.wait_ready(25s), "external capture converges through the shared runtime");
    require(follower->correction().correction_id > 0, "external controls actually execute");
    require(follower->correction().state == "restored", "readiness follows observed restoration");
    const auto before = follower->sequence.load();
    const auto control_identity = follower->binding_identity();
    session.stop(); session.stop();
    require(session.status().state == "stopped" && session.status().fault.empty(), "external synchronization closes cleanly");
    require(reference->running && follower->running && reference->starts == 1 && follower->starts == 1,
            "stop leaves user-owned acquisition running");
    await([&] { return follower->sequence > before + 2; }, 1s, "capture continues after synchronization stop");
    rejects([&] { session.set_node_addresses({}); });
    rejects([&] { session.set_node_addresses({{"a", "invalid"}}); });
    session.set_node_addresses({{"a", "127.0.0.1"}});
    session.start();
    require(session.wait_ready(10s) && follower->binds == 2 &&
                follower->binding_identity() == control_identity, "restart binds a new round without restarting capture");
    session.stop();
    require(reference->starts == 1 && follower->starts == 1, "sync restart preserves camera lifetime");
}
void active_stop_and_feedback() {
    auto config = group();
    auto reference = std::make_shared<Capture>("reference");
    auto follower = std::make_shared<Capture>("follower", 4000000);
    reference->start_capture(); follower->start_capture();
    SyncSession session(config, "a", {{"reference", reference}, {"follower", follower}});
    session.start();
    await([&] { return follower->correction().state == "submitting"; }, 8s, "external correction becomes active");
    session.stop();
    require(follower->correction().state == "restored" && follower->running,
            "stop waits for baseline completion without stopping capture");
    require(session.status().fault.empty(), "acknowledged restoration closes cleanly");

    config.request_timeout_seconds = .2;
    auto held = std::make_shared<Capture>("follower", 4000000);
    held->hold_restoration = true;
    held->start_capture();
    SyncSession missing(config, "a", {{"reference", reference}, {"follower", held}});
    missing.start();
    await([&] { return held->correction().state == "settling"; }, 8s, "capture submits restoration without acknowledging it");
    require(!missing.status().ready, "submission is not completion");
    missing.stop();
    require(missing.status().fault.find("restoration deadline") != std::string::npos && held->running,
            "missing completion is reported while capture remains owned by the application");
    held->hold_restoration = false;
    auto idle = std::make_shared<Capture>("follower", 4000000);
    idle->start_capture();
    SyncSession no_evidence(config, "a", {{"reference", reference}, {"follower", idle}});
    no_evidence.start();
    await([&] { return idle->correction().state == "submitting"; }, 8s, "idle-feedback fixture receives a command");
    idle->idle_feedback = true;
    no_evidence.stop();
    require(no_evidence.status().fault.find("invalid baseline restoration feedback") != std::string::npos,
            "idle state after a correction cannot replace observed restoration");
}
void callback_errors_and_identity() {
    for (bool bad_ack : {false, true}) {
        auto config = group();
        auto reference = std::make_shared<Capture>("reference");
        auto follower = std::make_shared<Capture>("follower", 4000000);
        follower->wrong_ack = bad_ack;
        follower->fail_snapshot = !bad_ack;
        reference->start_capture(); follower->start_capture();
        SyncSession session(config, "a", {{"reference", reference}, {"follower", follower}});
        session.start();
        rejects([&] { session.wait_ready(10s); });
        require(session.status().fault.find(bad_ack ? "acknowledgement" : "snapshot failure") != std::string::npos,
                "adapter errors and wrong command identities reach synchronization status");
        require(reference->running && follower->running, "synchronization faults preserve acquisition");
        follower->fail_snapshot = false;
        session.stop();
    }
    const auto config = group();
    auto reference = std::make_shared<Capture>("reference");
    rejects([&] { SyncSession invalid(config, "a", {{"reference", reference}}); });
    rejects([&] { SyncSession invalid(config, "a", {{"reference", reference}, {"follower", nullptr}}); });
}
void binding_cleanup() {
    auto config = group();
    auto reference = std::make_shared<Capture>("reference");
    auto follower = std::make_shared<Capture>("follower");
    reference->start_capture(); follower->start_capture();
    SyncSession session(config, "a", {{"reference", reference}, {"follower", follower}});
    for (unsigned i = 0; i < 20; ++i) {
        session.start(); session.stop();
        require(session.status().fault.empty(), "immediate stop cannot cancel unbound controls");
    }
    require(reference->cancels == reference->binds && follower->cancels == follower->binds,
            "cleanup corresponds only to successfully bound rounds");
    reference->fail_bind = true;
    session.start();
    rejects([&] { session.wait_ready(2s); });
    session.stop();
    require(reference->cancels == reference->binds && follower->cancels == follower->binds,
            "partial binding failure releases only successful interfaces");
}
void remote_identity_and_disconnect() {
    auto config = group();
    config.clock_mode = "host";
    config.nodes[0].address = "127.0.0.1";
    config.nodes.push_back({"b", "127.0.0.1", "", free_port()});
    config.units = {{"reference", "a"}, {"first", "b"}, {"second", "b"}};
    auto reference = std::make_shared<Capture>("reference");
    auto first = std::make_shared<Capture>("first"), second = std::make_shared<Capture>("second");
    reference->start_capture(); first->start_capture(); second->start_capture();
    SyncSession follower(config, "b", {{"first", second}, {"second", first}});
    SyncSession coordinator(config, "a", {{"reference", reference}});
    follower.start(); coordinator.start();
    rejects([&] { coordinator.wait_ready(4s); });
    require(coordinator.status().fault.find("snapshot unit identity") != std::string::npos,
            "remote ports cannot swap unit identities even on the same node");
    coordinator.stop(); follower.stop();

    config.units.pop_back();
    config.request_timeout_seconds = 5;
    SyncSession failed(config, "b", {{"first", first}});
    failed.start();
    std::unique_ptr<detail::Connection> channel;
    await([&] {
        try { channel = detail::connect(config.node("b"), .1); return true; }
        catch (const std::exception&) { return false; }
    }, 2s, "follower opens its synchronization endpoint");
    channel->timeout(5);
    channel->send({{"request_id", 1}, {"command", "hello"}, {"session", "test-peer"},
                   {"topology", detail::topology_json(config)}});
    require(channel->receive().at("ok").get<bool>(), "test peer establishes a session");
    const auto before = std::chrono::steady_clock::now();
    first->capture_fault = true;
    await([&] { return !failed.status().fault.empty(); }, 2s, "periodic follower fault is detected");
    channel->send({{"request_id", 2}, {"command", "snapshots"}, {"session", "test-peer"}});
    const auto response = channel->receive();
    require(response.at("stop_requested").get<bool>() && response.at("stop_reason") == "fault" &&
                response.at("fault").get<std::string>().find("external capture fault") != std::string::npos,
            "follower reports its fault before closing the control channel");
    channel.reset();
    require(std::chrono::steady_clock::now() - before < 2s,
            "periodic follower fault reaches the coordinator before the request timeout");
    require(first->running, "transport fault preserves external capture");
    first->capture_fault = false;
    failed.stop();
}
void supplied_phase_policy() {
    auto config = group();
    config.phase_policy.startup_delta_us = 100;
    config.phase_policy.tracking_delta_us = 100;
    config.phase_policy.evaluation_seconds = .1;
    auto reference = std::make_shared<Capture>("reference");
    auto follower = std::make_shared<Capture>("follower", 2000000);
    follower->control_margin_us = 100;
    reference->start_capture(); follower->start_capture();
    SyncSession session(config, "a", {{"reference", reference}, {"follower", follower}});
    session.start();
    await([&] { return follower->snapshots > 5; }, 1s,
          "runtime observations honor a supplied evaluation interval below one second");
    require(session.wait_ready(25s), "supplied policy converges through a narrow external actuator");
    require(follower->correction().correction_id > 0 && follower->largest_delta_us > 0 &&
                follower->largest_delta_us <= 100,
            "runtime corrections respect the supplied actuator margin");
    session.stop();
    require(session.status().fault.empty() && follower->running, "narrow policy restores without capture ownership");

    auto invalid = config;
    invalid.phase_policy.startup_delta_us = 1001;
    const auto binds = follower->binds.load();
    rejects([&] { SyncSession rejected(invalid, "a", {{"reference", reference}, {"follower", follower}}); });
    require(follower->binds == binds, "invalid policy is rejected before callbacks begin");
}
void mapping_policy_intersection() {
    for (const auto span : {50000LL, 200000LL}) {
        auto config = group();
        config.phase_policy.max_mapping_span_ns = span;
        auto reference = std::make_shared<Capture>("reference");
        auto follower = std::make_shared<Capture>("follower");
        reference->mapping_span_ns = follower->mapping_span_ns = span == 50000 ? 75000 : 150000;
        reference->start_capture(); follower->start_capture();
        SyncSession session(config, "a", {{"reference", reference}, {"follower", follower}});
        session.start();
        rejects([&] { session.wait_ready(3s); });
        require(session.status().fault.find("mapping uncertainty") != std::string::npos,
                "phase policy retains both supplied and configured clock uncertainty bounds");
        session.stop();
    }
}
void mixed_nodes() {
    Config managed;
    managed.backend = "fake";
    managed.clock_mode = "host";
    managed.group_id = "mixed-integration";
    managed.reference_unit = "reference";
    managed.nodes = {{"a", "127.0.0.1", "", free_port()}, {"b", "127.0.0.1", "", free_port()}};
    UnitConfig a, b;
    a.id = "reference"; a.node_id = "a"; a.camera_id = "a-camera";
    b.id = "follower"; b.node_id = "b"; b.camera_id = "b-camera";
    managed.units = {a, b};
    auto capture = std::make_shared<Capture>("follower", 2000000);
    capture->start_capture();
    SyncSession external(managed.sync_config(), "b", {{"follower", capture}});
    Session reference(managed, "a");
    external.start(); reference.start();
    require(reference.wait_ready(25s) && external.wait_ready(2s), "managed and external nodes share one protocol and coordinator");
    const auto mapping = external.clock();
    require(mapping.valid && mapping.map(boot_time_ns()) > boot_time_ns(), "external users can map their frame timestamps");
    reference.stop();
    await([&] { return external.status().state == "stopped"; }, 2s, "coordinator stop ends external synchronization");
    const auto before = capture->sequence.load();
    await([&] { return capture->sequence > before + 2; }, 1s, "remote group stop leaves external capture running");
    require(external.status().fault.empty() && capture->starts == 1, "mixed group closes without taking camera ownership");
    external.stop();
}
} // namespace
int main() {
    try {
        application_ownership(); supplied_phase_policy(); mapping_policy_intersection(); active_stop_and_feedback(); callback_errors_and_identity(); binding_cleanup(); remote_identity_and_disconnect(); mixed_nodes();
        std::cout << "external synchronization tests passed\n";
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
