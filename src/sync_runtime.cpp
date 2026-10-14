#include "sync_runtime.hpp"
#include "bcamsync/phase.hpp"
#include "platform.hpp"
#include "wire.hpp"
#include <algorithm>
#include <arpa/inet.h>
#include <atomic>
#include <cmath>
#include <condition_variable>
#include <ctime>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <mutex>
#include <poll.h>
#include <random>
#include <set>
#include <sstream>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>
namespace bcamsync::detail {
namespace {
std::string identity() {
    std::random_device r;
    std::ostringstream s;
    for (int i = 0; i < 4; ++i)
        s << std::hex << std::setw(8) << std::setfill('0') << r();
    return s.str();
}
std::int64_t shifted(std::int64_t value, std::int64_t offset) {
    if ((offset > 0 && value > std::numeric_limits<std::int64_t>::max() - offset) ||
        (offset < 0 && value < std::numeric_limits<std::int64_t>::min() - offset))
        throw std::runtime_error("clock mapping exceeds timestamp range");
    return value + offset;
}
std::int64_t unshifted(std::int64_t value, std::int64_t offset) {
    if ((offset > 0 && value < std::numeric_limits<std::int64_t>::min() + offset) ||
        (offset < 0 && value > std::numeric_limits<std::int64_t>::max() + offset))
        throw std::runtime_error("clock mapping exceeds timestamp range");
    return value - offset;
}
} // namespace
class SyncRuntime::Impl {
  public:
    SyncConfig config, resolved;
    std::string local, session, control_session = bcamsync::detail::identity();
    std::uint64_t round_id = 0;
    LocalSyncUnits ports;
    std::set<std::string> bound;
    std::vector<SyncUnitConfig> units;
    bool common = false, coordinator = false;
    detail::NodePlatform platform;
    detail::ClockEstimate clock;
    mutable std::mutex mutex;
    std::condition_variable changed;
    GroupStatus group;
    std::atomic<bool> stopping{true};
    int server = -1;
    std::map<std::string, std::unique_ptr<detail::Connection>> peers;
    std::map<std::string, std::uint64_t> requests, corrections;
    bool units_cleaned = false;
    std::unique_ptr<detail::Connection> follower_connection;
    std::uint64_t stop_request_id = 0;
    std::int64_t startup_deadline = 0, clock_sampled_at = 0;
    Impl(SyncConfig c, std::string n, LocalSyncUnits ps)
        : config(std::move(c)), resolved(config), local(std::move(n)), ports(std::move(ps)), platform(config, local) {
        config.validate();
        config.node(local);
        for (const auto& u : config.units)
            if (u.node_id == local) units.push_back(u);
        if (ports.size() != units.size())
            throw std::invalid_argument("sync interfaces do not match local topology");
        for (const auto& u : units)
            if (!ports.count(u.id) || !ports.at(u.id))
                throw std::invalid_argument("missing local sync interface: " + u.id);
        common = config.nodes.size() > 1;
        coordinator = config.reference_node() == local;
    }
    SyncUnit& port(const std::string& id) const {
        const auto found = ports.find(id);
        if (found == ports.end()) throw std::invalid_argument("unit is not local: " + id);
        return *found->second;
    }
    void publish(GroupStatus s) {
        std::lock_guard<std::mutex> lock(mutex);
        group = std::move(s);
        changed.notify_all();
    }
    GroupStatus status() const {
        std::lock_guard<std::mutex> lock(mutex);
        return group;
    }
    void fail(const std::string& reason) {
        auto s = status();
        s.state = "fault";
        s.ready = false;
        if (s.fault.empty()) s.fault = reason;
        else s.fault += "; " + reason;
        for (auto& u : s.units)
            u.ready = false;
        publish(s);
    }
    void health() {
        for (const auto& entry : ports) {
            const auto fault = entry.second->fault();
            if (!fault.empty()) throw std::runtime_error(entry.first + ": " + fault);
        }
        update_clock();
    }
    detail::Json rpc(const std::string& node, detail::Json message) {
        auto& id = requests[node];
        message["request_id"] = ++id;
        message["session"] = session;
        peers.at(node)->send(message);
        auto response = peers.at(node)->receive();
        if (response.at("request_id").get<std::uint64_t>() != id)
            throw std::runtime_error("out-of-order control response");
        if (!response.at("ok").get<bool>())
            throw std::runtime_error(response.at("error").get<std::string>());
        return response.at("result");
    }
    detail::ClockEstimate clock_estimate() const {
        std::lock_guard<std::mutex> lock(mutex);
        auto estimate = clock;
        if (common && (clock_sampled_at == 0 ||
            monotonic_ns() - clock_sampled_at > static_cast<std::int64_t>(config.clock_max_age_seconds * 1e9)))
            estimate.valid = false;
        return estimate;
    }
    void update_clock() {
        detail::ClockEstimate estimate;
        try { estimate = platform.sample(); }
        catch (const std::exception& error) { estimate.fault = error.what(); }
        {
            std::lock_guard<std::mutex> lock(mutex);
            clock = estimate;
            clock_sampled_at = monotonic_ns();
        }
        if (!estimate.valid)
            throw std::runtime_error(estimate.fault.empty() ? "common clock is unavailable" : estimate.fault);
    }
    std::int64_t common_now() const {
        const auto estimate = clock_estimate();
        if (!estimate.valid) throw std::runtime_error("common clock is unavailable");
        return shifted(boot_time_ns(), estimate.offset_ns);
    }
    TimingSnapshot snapshot(const std::string& id) const {
        auto result = port(id).snapshot();
        if (result.unit_id != id) throw std::runtime_error("snapshot unit identity differs");
        return result;
    }
    TimingSnapshot mapped_snapshot(const std::string& id) const {
        auto result = snapshot(id);
        const auto estimate = clock_estimate();
        if (!estimate.valid) throw std::runtime_error("common clock is unavailable");
        result.snapshot_timestamp_ns = shifted(result.snapshot_timestamp_ns, estimate.offset_ns);
        for (auto& frame : result.frames) {
            frame.timestamp_ns = shifted(frame.timestamp_ns, estimate.offset_ns);
            frame.mapping_span_ns = shifted(frame.mapping_span_ns, estimate.uncertainty_ns);
        }
        if (result.correction.restored_timestamp_ns)
            result.correction.restored_timestamp_ns = shifted(result.correction.restored_timestamp_ns, estimate.offset_ns);
        return result;
    }
    CorrectionStatus adjust_local(const std::string& unit, Pulse pulse) {
        const auto estimate = clock_estimate();
        if (!estimate.valid) throw std::runtime_error("common clock is unavailable");
        pulse.expires_ns = unshifted(pulse.expires_ns, estimate.offset_ns);
        const auto result = port(unit).adjust(pulse);
        if (result.correction_id != pulse.correction_id ||
            (result.state != "submitting" && result.state != "settling" &&
             result.state != "restored"))
            throw std::runtime_error("correction acknowledgement differs");
        corrections[unit] = pulse.correction_id;
        return result;
    }
    void cancel_units() {
        if (units_cleaned) return;
        units_cleaned = true;
        std::vector<std::string> pending;
        for (const auto& id : bound) {
            try { port(id).cancel(); pending.push_back(id); }
            catch (const std::exception& error) { fail(id + ": " + error.what()); }
        }
        const auto deadline = shifted(monotonic_ns(), static_cast<std::int64_t>(config.request_timeout_seconds * 1e9));
        while (!pending.empty()) {
            for (auto it = pending.begin(); it != pending.end();) {
                try {
                    const auto observed = snapshot(*it);
                    const auto state = observed.correction;
                    if (corrections.count(*it) && state.correction_id != corrections.at(*it))
                        throw std::runtime_error("restoration correction identity differs");
                    if (state.state == "submitting" || state.state == "settling") {
                        if (monotonic_ns() >= deadline) {
                            fail(*it + ": baseline restoration deadline exceeded");
                            it = pending.erase(it);
                        } else ++it;
                    } else if ((state.state == "idle" && !corrections.count(*it)) ||
                               ((state.state == "restored" || state.state == "cancelled") &&
                                state.restored_timestamp_ns > 0 &&
                                state.restored_timestamp_ns <= observed.snapshot_timestamp_ns)) {
                        it = pending.erase(it);
                    } else {
                        fail(*it + ": invalid baseline restoration feedback");
                        it = pending.erase(it);
                    }
                } catch (const std::exception& error) {
                    fail(*it + ": " + error.what());
                    it = pending.erase(it);
                }
            }
            if (!pending.empty()) std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
    }
    void suspend_synchronization() {
        if (follower_connection) follower_connection->shutdown();
        cancel_units();
        for (auto& peer : peers) peer.second->shutdown();
        peers.clear();
        while (!stopping) {
            try { update_clock(); } catch (const std::exception&) {}
            std::unique_lock<std::mutex> lock(mutex);
            changed.wait_for(lock, std::chrono::seconds(1), [&] { return stopping.load(); });
        }
    }
    void begin() {
        if (round_id == std::numeric_limits<std::uint64_t>::max())
            throw std::runtime_error("synchronization round exhausted");
        ++round_id;
        stopping = false;
        units_cleaned = false;
        resolved = config;
        stop_request_id = 0;
        corrections.clear();
        bound.clear();
        { std::lock_guard<std::mutex> clock_lock(mutex); clock = {!common, 0, 0, {}}; clock_sampled_at = 0; }
        startup_deadline = shifted(monotonic_ns(), static_cast<std::int64_t>(config.startup_timeout_seconds * 1e9));
        GroupStatus initial;
        initial.state = "starting";
        publish(initial);
        session = coordinator ? control_session + "-" + std::to_string(round_id) : std::string{};
    }
    void run() {
        try {
            for (const auto& entry : ports) {
                if (stopping) break;
                entry.second->bind(control_session, round_id);
                bound.insert(entry.first);
            }
            if (!stopping) platform.start(resolved, stopping, startup_deadline);
            if (!stopping) update_clock();
            if (!stopping) {
                if (coordinator) run_coordinator();
                else run_follower();
            }
        } catch (const std::exception& error) { if (!stopping) fail(error.what()); }
        catch (...) { if (!stopping) fail("unknown synchronization failure"); }
        if (!stopping) suspend_synchronization();
        if (coordinator) {
            for (const auto& entry : peers) {
                try {
                    const auto reply = rpc(entry.first, {{"command", "stop"}});
                    if (!reply.at("stopped").get<bool>())
                        fail(entry.first + ": " + reply.at("fault").get<std::string>());
                } catch (const std::exception& error) {
                    fail(entry.first + ": shutdown acknowledgement failed: " + error.what());
                }
            }
        }
        cancel_units();
    }
    void request_stop() { stopping = true; changed.notify_all(); }
    void wait_for_stop() {
        std::unique_lock<std::mutex> lock(mutex);
        changed.wait(lock, [&] { return stopping.load(); });
    }
    void finish() {
        // The owning access layer has completed its cleanup before acknowledging group stop.
        if (stop_request_id && follower_connection) {
            const auto final = status();
            try { follower_connection->send({{"request_id", stop_request_id}, {"ok", true},
                    {"result", {{"stopped", final.fault.empty()}, {"fault", final.fault}}}}); }
            catch (const std::exception& error) { fail(error.what()); }
            stop_request_id = 0;
        }
        follower_connection.reset();
        for (auto& entry : peers) entry.second->shutdown();
        peers.clear();
        requests.clear();
        if (server >= 0) { close(server); server = -1; }
        try { platform.stop(); } catch (const std::exception& error) { fail(error.what()); }
        { std::lock_guard<std::mutex> lock(mutex); clock.valid = false; }
        auto final = status();
        final.ready = false;
        for (auto& unit : final.units) unit.ready = false;
        if (final.fault.empty()) final.state = "stopped";
        publish(final);
    }
    void run_follower() {
        server = detail::listener(resolved.node(local));
        int client = -1;
        auto next_health = monotonic_ns();
        while (!stopping) {
            if (monotonic_ns() >= next_health) {
                health();
                next_health = monotonic_ns() + 1000000000LL;
            }
            pollfd p{server, POLLIN, 0};
            const int r = poll(&p, 1, 100);
            if (r > 0) {
                client = accept(server, nullptr, nullptr);
                if (client >= 0)
                    break;
            }
            if (monotonic_ns() > startup_deadline)
                throw std::runtime_error("coordinator connection startup timeout");
        }
        if (stopping) {
            if (client >= 0)
                close(client);
            return;
        }
        try {
            follower_connection =
                std::make_unique<detail::Connection>(client, config.request_timeout_seconds);
        } catch (...) {
            close(client);
            throw;
        }
        auto& channel = *follower_connection;
        std::uint64_t expected = 1;
        auto last_command = monotonic_ns();
        const auto idle_limit =
            static_cast<std::int64_t>((2.0 + config.request_timeout_seconds *
                                                 (static_cast<double>(config.nodes.size()) + 1.0)) *
                                      1e9);
        while (!stopping) {
            if (monotonic_ns() >= next_health) {
                health();
                next_health = monotonic_ns() + 1000000000LL;
            }
            pollfd incoming_fd{client, POLLIN, 0};
            const auto event = poll(&incoming_fd, 1, 100);
            if (event < 0 && errno == EINTR)
                continue;
            if (event < 0)
                throw std::runtime_error("coordinator poll failed");
            if (event == 0) {
                if (monotonic_ns() - last_command > idle_limit)
                    throw std::runtime_error("coordinator heartbeat deadline exceeded");
                continue;
            }
            detail::Json incoming = channel.receive();
            last_command = monotonic_ns();
            const auto id = incoming.at("request_id").get<std::uint64_t>();
            detail::Json response;
            try {
                if (id != expected++)
                    throw std::runtime_error("out-of-order command");
                const std::string command = incoming.at("command");
                detail::Json result;
                if (session.empty()) {
                    if (command != "hello" ||
                        incoming.at("topology") != detail::topology_json(config))
                        throw std::runtime_error("group synchronization configuration differs");
                    session = incoming.at("session").get<std::string>();
                    if (session.empty() || session.size() > 128)
                        throw std::runtime_error("invalid session identity");
                    result = {{"node", local}};
                } else {
                    if (incoming.at("session").get<std::string>() != session)
                        throw std::runtime_error("session identity differs");
                    if (command == "snapshots") {
                        health();
                        detail::Json snapshots = detail::Json::array();
                        for (auto& entry : ports)
                            snapshots.push_back(detail::snapshot_json(mapped_snapshot(entry.first)));
                        result = {{"node", local},
                                  {"snapshots", snapshots},
                                  {"checked_at", realtime_ns()}};
                    } else if (command == "adjust") {
                        const std::string id_unit = incoming.at("unit");
                        if (id_unit == config.reference_unit)
                            throw std::runtime_error("reference unit cannot be adjusted");
                        Pulse p{incoming.at("id"), incoming.at("delta"), incoming.at("count"),
                                incoming.at("expires")};
                        const auto ack = adjust_local(id_unit, p);
                        result = {{"id", ack.correction_id}, {"state", ack.state}};
                    } else if (command == "status") {
                        auto s = detail::status_from_json(incoming.at("status"));
                        publish(s);
                        result = {{"stored", true}};
                    } else if (command == "stop") {
                        request_stop();
                        stop_request_id = id;
                        return;
                    } else
                        throw std::runtime_error("unknown control command");
                }
                response = {{"request_id", id}, {"ok", true}, {"result", result}};
            } catch (const std::exception& e) {
                channel.send({{"request_id", id}, {"ok", false}, {"error", e.what()}});
                throw;
            }
            channel.send(response);
        }
    }
    void run_coordinator() {
        const auto deadline = startup_deadline;
        for (const auto& n : resolved.nodes) {
            if (n.id == local)
                continue;
            while (!stopping) {
                try {
                    peers[n.id] = detail::connect(n, config.request_timeout_seconds);
                    break;
                } catch (const std::exception&) {
                    if (monotonic_ns() > deadline)
                        throw std::runtime_error("node connection startup timeout: " + n.id);
                    std::cerr << "Waiting for node " << n.id << std::endl;
                    std::unique_lock<std::mutex> lock(mutex);
                    changed.wait_for(lock, std::chrono::milliseconds(250),
                                     [&] { return stopping.load(); });
                }
            }
            if (stopping)
                return;
            const double remaining = static_cast<double>(deadline - monotonic_ns()) / 1e9;
            if (remaining <= 0)
                throw std::runtime_error("node initialization startup timeout: " + n.id);
            peers.at(n.id)->timeout(std::min(remaining, config.request_timeout_seconds));
            const auto hello = rpc(n.id, {{"command", "hello"},
                                          {"topology", detail::topology_json(config)}});
            peers.at(n.id)->timeout(config.request_timeout_seconds);
            if (hello.at("node") != n.id)
                throw std::runtime_error("peer node identity differs");
        }
        auto policy = config.phase_policy;
        policy.max_mapping_span_ns = std::min(policy.max_mapping_span_ns,
            static_cast<std::int64_t>(std::ceil(config.clock_max_offset_ms * 1000000)));
        std::map<std::string, PhaseController> controllers;
        for (const auto& u : config.units)
            if (u.id != config.reference_unit) {
                controllers.emplace(u.id, PhaseController(policy, 1000000000LL / config.fps));
                controllers.at(u.id).reset(monotonic_ns(), common_now());
            }
        std::int64_t next = 0;
        const auto calibration_deadline = startup_deadline;
        std::map<std::string, UnitStatus> last_metrics;
        while (!stopping) {
            if (monotonic_ns() < next) {
                std::unique_lock<std::mutex> lock(mutex);
                changed.wait_for(lock, std::chrono::milliseconds(20),
                                 [&] { return stopping.load(); });
                continue;
            }
            health();
            std::map<std::string, TimingSnapshot> snapshots;
            for (const auto& entry : ports) {
                auto s = mapped_snapshot(entry.first);
                if (s.unit_id != entry.first)
                    throw std::runtime_error("local snapshot unit identity differs");
                snapshots.emplace(s.unit_id, std::move(s));
            }
            for (const auto& n : config.nodes) {
                if (n.id == local)
                    continue;
                const auto result = rpc(n.id, {{"command", "snapshots"}});
                if (result.at("node") != n.id)
                    throw std::runtime_error("snapshot node identity differs");
                for (const auto& j : result.at("snapshots")) {
                    auto s = detail::snapshot_from_json(j);
                    if (config.unit(s.unit_id).node_id != n.id ||
                        !snapshots.emplace(s.unit_id, std::move(s)).second)
                        throw std::runtime_error("snapshot unit identity differs");
                }
            }
            if (snapshots.size() != config.units.size())
                throw std::runtime_error("incomplete group timing snapshots");
            GroupStatus s;
            s.state = "calibrating";
            s.ready = true;
            for (const auto& u : config.units) {
                UnitStatus us = last_metrics[u.id];
                us.unit_id = u.id;
                if (u.id == config.reference_unit) {
                    us.state = "reference";
                    us.ready = false;
                    try {
                        validate_timing(snapshots.at(u.id), common_now(), policy);
                        us.ready = true;
                    } catch (const WindowPending&) {
                        if (status().ready)
                            throw std::runtime_error("reference timing missing");
                    }
                    s.ready = s.ready && us.ready;
                } else {
                    const auto d = controllers.at(u.id).tick(snapshots.at(config.reference_unit),
                                                             snapshots.at(u.id), common_now(),
                                                             monotonic_ns());
                    us.state = d.state;
                    us.ready = d.ready;
                    us.fault = d.fault;
                    if (d.measurement) {
                        us.phase_ms = d.measurement->phase_ns / 1e6;
                        us.p95_ms = d.measurement->absolute_p95_ns / 1e6;
                        us.max_ms = d.measurement->absolute_max_ns / 1e6;
                        us.measurement_end_ns = d.measurement->window_end_ns;
                    }
                    if (!d.fault.empty())
                        throw std::runtime_error(u.id + ": " + d.fault);
                    s.ready = s.ready && d.ready;
                    if (d.action) {
                        auto pulse = *d.action;
                        pulse.expires_ns = shifted(common_now(), 1000000000LL);
                        if (u.node_id == local)
                            adjust_local(u.id, pulse);
                        else {
                            const auto ack = rpc(u.node_id, {{"command", "adjust"},
                                                             {"unit", u.id},
                                                             {"id", pulse.correction_id},
                                                             {"delta", pulse.delta_us},
                                                             {"count", pulse.count},
                                                             {"expires", pulse.expires_ns}});
                            if (ack.at("id").get<std::uint64_t>() != pulse.correction_id)
                                throw std::runtime_error("correction acknowledgement differs");
                        }
                    }
                }
                last_metrics[u.id] = us;
                s.units.push_back(us);
            }
            if (s.ready)
                s.state = "tracking";
            else if (monotonic_ns() > calibration_deadline)
                throw std::runtime_error("group startup calibration deadline exceeded");
            publish(s);
            for (const auto& n : config.nodes)
                if (n.id != local)
                    rpc(n.id, {{"command", "status"}, {"status", detail::status_json(s)}});
            next = monotonic_ns() + static_cast<std::int64_t>(std::min(1.0, policy.evaluation_seconds) * 1e9);
        }
    }
};
SyncRuntime::SyncRuntime(SyncConfig config, std::string node, LocalSyncUnits units)
    : impl_(std::make_unique<Impl>(std::move(config), std::move(node), std::move(units))) {}
SyncRuntime::~SyncRuntime() = default;
void SyncRuntime::begin() { impl_->begin(); }
void SyncRuntime::set_node_addresses(const std::map<std::string, std::string>& addresses) {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    auto config = impl_->config;
    if (addresses.size() != config.nodes.size())
        throw std::invalid_argument("addresses must identify every configured node");
    for (auto& node : config.nodes) {
        const auto found = addresses.find(node.id);
        if (found == addresses.end())
            throw std::invalid_argument("missing node address: " + node.id);
        in_addr address{};
        if (inet_pton(AF_INET, found->second.c_str(), &address) != 1 ||
            ntohl(address.s_addr) == 0 || (ntohl(address.s_addr) >> 28) >= 14)
            throw std::invalid_argument("node address must be a unicast IPv4 address");
        node.address = found->second;
    }
    config.network_mode = "existing";
    config.validate();
    impl_->config = std::move(config);
}
void SyncRuntime::run() { impl_->run(); }
void SyncRuntime::request_stop() { impl_->request_stop(); }
bool SyncRuntime::stopping() const { return impl_->stopping.load(); }
void SyncRuntime::wait_for_stop() { impl_->wait_for_stop(); }
void SyncRuntime::finish() { impl_->finish(); }
void SyncRuntime::fail(const std::string& reason) { impl_->fail(reason); }
const std::string& SyncRuntime::identity() const { return impl_->control_session; }
std::uint64_t SyncRuntime::round() const { return impl_->round_id; }
GroupStatus SyncRuntime::status() const { return impl_->status(); }
ClockMapping SyncRuntime::clock() const {
    const auto estimate = impl_->clock_estimate();
    return {estimate.valid, estimate.offset_ns, estimate.uncertainty_ns};
}
bool SyncRuntime::wait_ready(std::chrono::milliseconds timeout) {
    std::unique_lock<std::mutex> lock(impl_->mutex);
    impl_->changed.wait_for(lock, timeout, [&] {
        return impl_->group.ready || !impl_->group.fault.empty() || impl_->stopping.load();
    });
    if (!impl_->group.fault.empty()) throw std::runtime_error(impl_->group.fault);
    return impl_->group.ready;
}
} // namespace bcamsync::detail
