#include "bcamsync/session.hpp"
#include "bcamsync/phase.hpp"
#include "process.hpp"
#include "wire.hpp"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <condition_variable>
#include <ctime>
#include <iomanip>
#include <iostream>
#include <locale>
#include <map>
#include <mutex>
#include <poll.h>
#include <random>
#include <sstream>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>
namespace bcamsync {
namespace {
std::string identity() {
    std::random_device r;
    std::ostringstream s;
    for (int i = 0; i < 4; ++i)
        s << std::hex << std::setw(8) << std::setfill('0') << r();
    return s.str();
}
std::string trim(const std::string& s) {
    const auto a = s.find_first_not_of(" \t\r\n"), b = s.find_last_not_of(" \t\r\n");
    return a == std::string::npos ? std::string{} : s.substr(a, b - a + 1);
}
double number(const std::string& text) {
    std::istringstream input(text);
    input.imbue(std::locale::classic());
    double result = 0;
    if (!(input >> result) || !std::isfinite(result))
        throw std::runtime_error("invalid numeric clock report");
    return result;
}
std::int64_t domain_now(bool common) {
    if (common)
        return realtime_ns();
#ifdef CLOCK_BOOTTIME
    timespec t{};
    if (clock_gettime(CLOCK_BOOTTIME, &t))
        throw std::runtime_error("cannot read capture clock");
    return static_cast<std::int64_t>(t.tv_sec) * 1000000000LL + t.tv_nsec;
#else
    return monotonic_ns();
#endif
}
std::vector<UnitConfig> local_units(const Config& c, const std::string& node) {
    std::vector<UnitConfig> us;
    for (const auto& u : c.units)
        if (u.node_id == node)
            us.push_back(u);
    return us;
}
std::vector<std::unique_ptr<AcquisitionBackend>> validated_backends(const Config& c,
                                                                    const std::string& node) {
    c.validate();
    c.node(node);
    return make_arducam_backends(local_units(c, node));
}
void clock_health(const Config& c, const std::string& node) {
    std::istringstream lines(
        detail::run_program({"chronyc", "-n", "tracking"}, c.request_timeout_seconds));
    lines.imbue(std::locale::classic());
    std::map<std::string, std::string> v;
    std::string line;
    while (std::getline(lines, line)) {
        const auto p = line.find(':');
        if (p != std::string::npos)
            v[trim(line.substr(0, p))] = trim(line.substr(p + 1));
    }
    if (v.at("Leap status") != "Normal" || std::stoi(v.at("Stratum")) <= 0)
        throw std::runtime_error("chrony is not synchronized");
    const auto correction = std::abs(number(v.at("System time"))) * 1000,
               offset = std::abs(number(v.at("Last offset"))) * 1000;
    if (!std::isfinite(correction) || !std::isfinite(offset) ||
        std::max(correction, offset) > c.clock_max_offset_ms)
        throw std::runtime_error("clock correction/offset exceeds gate");
    const auto reference = v.at("Reference ID");
    const bool local = reference.substr(0, 8) == "7F7F0101";
    if (node == c.reference_node() && !local)
        throw std::runtime_error("reference node is not using its configured local clock");
    if (node != c.reference_node()) {
        const auto begin = reference.find('('), end = reference.find(')');
        if (local || begin == std::string::npos || end == std::string::npos ||
            reference.substr(begin + 1, end - begin - 1) != c.node(c.reference_node()).address)
            throw std::runtime_error("chrony did not select the group reference node");
    }
    if (!local) {
        std::tm sampled{};
        std::istringstream value(v.at("Ref time (UTC)"));
        value.imbue(std::locale::classic());
        value >> std::get_time(&sampled, "%a %b %d %H:%M:%S %Y");
        if (value.fail())
            throw std::runtime_error("invalid chrony reference time");
        const auto sampled_sec = timegm(&sampled);
        const double age =
            static_cast<double>(realtime_ns()) / 1e9 - static_cast<double>(sampled_sec);
        if (age < -1 || age > c.clock_max_age_seconds)
            throw std::runtime_error("chrony reference sample is stale");
    }
}
} // namespace
class Session::Impl {
  public:
    Config config;
    std::string local, session;
    std::vector<std::unique_ptr<AcquisitionBackend>> backends;
    std::vector<UnitConfig> units;
    bool fixture = false, common = false, coordinator = false;
    mutable std::mutex mutex;
    std::mutex lifecycle;
    std::condition_variable changed;
    GroupStatus group;
    std::atomic<bool> stopping{true};
    std::thread thread;
    int server = -1;
    std::map<std::string, std::unique_ptr<detail::Connection>> peers;
    std::map<std::string, std::uint64_t> requests;
    bool backends_cleaned = false;
    std::int64_t startup_deadline = 0;
    Impl(Config c, std::string n, std::vector<std::unique_ptr<AcquisitionBackend>> bs, bool f)
        : config(std::move(c)), local(std::move(n)), backends(std::move(bs)), fixture(f) {
        config.validate();
        config.node(local);
        units = local_units(config, local);
        if (backends.size() != units.size() ||
            std::any_of(backends.begin(), backends.end(), [](const auto& b) { return !b; }))
            throw std::invalid_argument("backend count/ownership does not match local topology");
        common = config.nodes.size() > 1;
        coordinator = config.reference_node() == local;
    }
    AcquisitionBackend& backend(const std::string& id) {
        for (std::size_t i = 0; i < units.size(); ++i)
            if (units[i].id == id)
                return *backends[i];
        throw std::invalid_argument("unit is not local: " + id);
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
        s.fault = reason;
        for (auto& u : s.units)
            u.ready = false;
        publish(s);
    }
    void health() {
        for (std::size_t i = 0; i < backends.size(); ++i) {
            auto f = backends[i]->fault();
            if (!f.empty())
                throw std::runtime_error(units[i].id + ": " + f);
        }
        if (common && !fixture)
            clock_health(config, local);
    }
    void start_backends() {
        for (auto& b : backends) {
            if (stopping)
                throw std::runtime_error("startup cancelled");
            b->start(session, 1, common);
        }
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
    void prepare_clock() {
        if (!common || fixture)
            return;
        std::cerr << "Preparing node clock " << local << std::endl;
        detail::run_program(
            {"sudo", "-n", "/usr/local/libexec/bcamsync-clock-" + config.group_id + "-" + local},
            config.startup_timeout_seconds + 10);
        clock_health(config, local);
    }
    void cleanup_backends() {
        if (backends_cleaned)
            return;
        backends_cleaned = true;
        for (std::size_t i = 0; i < backends.size(); ++i) {
            try {
                backends[i]->cancel();
            } catch (const std::exception& e) {
                fail(units[i].id + ": " + e.what());
            }
            try {
                backends[i]->stop();
                const auto error = backends[i]->fault();
                if (!error.empty())
                    fail(units[i].id + ": " + error);
            } catch (const std::exception& e) {
                fail(units[i].id + ": " + e.what());
            }
        }
        auto final = status();
        if (final.fault.empty()) {
            final.state = "stopped";
            final.ready = false;
            for (auto& u : final.units)
                u.ready = false;
            publish(final);
        }
    }
    void start() {
        std::lock_guard<std::mutex> lock(lifecycle);
        if (thread.joinable())
            throw std::runtime_error("session already started; stop before restarting");
        stopping = false;
        backends_cleaned = false;
        startup_deadline =
            monotonic_ns() + static_cast<std::int64_t>(config.startup_timeout_seconds * 1e9);
        GroupStatus initial;
        initial.state = "starting";
        publish(initial);
        session = coordinator ? identity() : std::string{};
        thread = std::thread([this] {
            try {
                prepare_clock();
                if (!stopping) {
                    if (coordinator)
                        run_coordinator();
                    else
                        run_follower();
                }
            } catch (const std::exception& e) {
                if (!stopping)
                    fail(e.what());
            } catch (...) {
                if (!stopping)
                    fail("unknown acquisition failure");
            }
            if (coordinator) {
                for (const auto& entry : peers) {
                    try {
                        const auto reply = rpc(entry.first, {{"command", "stop"}});
                        if (!reply.at("stopped").get<bool>())
                            fail(entry.first + ": " + reply.at("fault").get<std::string>());
                    } catch (const std::exception& e) {
                        fail(entry.first + ": shutdown acknowledgement failed: " + e.what());
                    }
                }
            }
            for (auto& entry : peers)
                entry.second->shutdown();
            cleanup_backends();
            changed.notify_all();
        });
    }
    void run_follower() {
        server = detail::listener(config.node(local));
        int client = -1;
        while (!stopping) {
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
        std::unique_ptr<detail::Connection> connection;
        try {
            connection =
                std::make_unique<detail::Connection>(client, config.request_timeout_seconds);
        } catch (...) {
            close(client);
            throw;
        }
        auto& channel = *connection;
        std::uint64_t expected = 1;
        auto last_command = monotonic_ns();
        const auto idle_limit =
            static_cast<std::int64_t>((2.0 + config.request_timeout_seconds *
                                                 (static_cast<double>(config.nodes.size()) + 1.0)) *
                                      1e9);
        while (!stopping) {
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
                        incoming.at("topology") != detail::topology_json(config) ||
                        incoming.at("fixture").get<bool>() != fixture)
                        throw std::runtime_error("group topology or backend mode differs");
                    session = incoming.at("session").get<std::string>();
                    if (session.empty() || session.size() > 128)
                        throw std::runtime_error("invalid session identity");
                    start_backends();
                    result = {{"node", local}};
                } else {
                    if (incoming.at("session").get<std::string>() != session)
                        throw std::runtime_error("session identity differs");
                    if (command == "snapshots") {
                        health();
                        detail::Json snapshots = detail::Json::array();
                        for (auto& b : backends)
                            snapshots.push_back(detail::snapshot_json(b->snapshot()));
                        result = {{"node", local},
                                  {"snapshots", snapshots},
                                  {"checked_at", realtime_ns()}};
                    } else if (command == "adjust") {
                        const std::string id_unit = incoming.at("unit");
                        if (id_unit == config.reference_unit)
                            throw std::runtime_error("reference unit cannot be adjusted");
                        Pulse p{incoming.at("id"), incoming.at("delta"), incoming.at("count"),
                                incoming.at("expires")};
                        const auto ack = backend(id_unit).adjust(p);
                        result = {{"id", ack.correction_id}, {"state", ack.state}};
                    } else if (command == "status") {
                        auto s = detail::status_from_json(incoming.at("status"));
                        publish(s);
                        result = {{"stored", true}};
                    } else if (command == "stop") {
                        stopping = true;
                        cleanup_backends();
                        const auto final = status();
                        result = {{"stopped", final.fault.empty()}, {"fault", final.fault}};
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
        start_backends();
        const auto deadline = startup_deadline;
        for (const auto& n : config.nodes) {
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
            peers.at(n.id)->timeout(remaining);
            const auto hello = rpc(n.id, {{"command", "hello"},
                                          {"topology", detail::topology_json(config)},
                                          {"fixture", fixture}});
            peers.at(n.id)->timeout(config.request_timeout_seconds);
            if (hello.at("node") != n.id)
                throw std::runtime_error("peer node identity differs");
        }
        std::map<std::string, PhaseController> controllers;
        for (const auto& u : config.units)
            if (u.id != config.reference_unit) {
                controllers.emplace(u.id, PhaseController({}, 1000000000LL / u.fps));
                controllers.at(u.id).reset(monotonic_ns(), domain_now(common));
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
            for (std::size_t i = 0; i < backends.size(); ++i) {
                auto s = backends[i]->snapshot();
                if (s.unit_id != units[i].id)
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
                        validate_timing(snapshots.at(u.id), domain_now(common));
                        us.ready = true;
                    } catch (const WindowPending&) {
                        if (status().ready)
                            throw std::runtime_error("reference timing missing");
                    }
                    s.ready = s.ready && us.ready;
                } else {
                    const auto d = controllers.at(u.id).tick(snapshots.at(config.reference_unit),
                                                             snapshots.at(u.id), domain_now(common),
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
                        pulse.expires_ns = domain_now(common) + 1000000000LL;
                        if (u.node_id == local)
                            backend(u.id).adjust(pulse);
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
            next = monotonic_ns() + 1000000000LL;
        }
    }
    void stop() {
        std::lock_guard<std::mutex> lock(lifecycle);
        stopping = true;
        changed.notify_all();
        if (thread.joinable())
            thread.join();
        if (server >= 0) {
            close(server);
            server = -1;
        }
        peers.clear();
        requests.clear();
        auto s = status();
        if (s.fault.empty()) {
            s.state = "stopped";
            s.ready = false;
            for (auto& u : s.units)
                u.ready = false;
            publish(s);
        }
    }
};
Session::Session(Config config, std::string node)
    : Session(config, node, validated_backends(config, node)) {
    impl_->fixture = false;
}
Session::Session(Config config, std::string node,
                 std::vector<std::unique_ptr<AcquisitionBackend>> bs)
    : impl_(std::make_unique<Impl>(std::move(config), std::move(node), std::move(bs), true)) {}
Session::~Session() {
    try {
        stop();
    } catch (...) {
    }
}
void Session::start() { impl_->start(); }
bool Session::wait_ready(std::chrono::milliseconds timeout) {
    std::unique_lock<std::mutex> lock(impl_->mutex);
    impl_->changed.wait_for(lock, timeout, [&] {
        return impl_->group.ready || !impl_->group.fault.empty() || impl_->stopping.load();
    });
    if (!impl_->group.fault.empty())
        throw std::runtime_error(impl_->group.fault);
    return impl_->group.ready;
}
std::shared_ptr<const FramePacket> Session::read(const std::string& id,
                                                 std::chrono::milliseconds timeout) {
    auto s = status();
    if (!s.fault.empty())
        throw std::runtime_error(s.fault);
    if (!s.ready)
        throw std::runtime_error("synchronization is not ready");
    return impl_->backend(id).read(timeout);
}
GroupStatus Session::status() const { return impl_->status(); }
void Session::stop() { impl_->stop(); }
} // namespace bcamsync
