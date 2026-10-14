#include "platform.hpp"
#include "process.hpp"
#include <algorithm>
#include <arpa/inet.h>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <locale>
#include <map>
#include <mutex>
#include <nlohmann/json.hpp>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <unistd.h>
#ifdef __linux__
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <poll.h>
#include <pwd.h>
#include <signal.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <systemd/sd-bus.h>
#endif

namespace bcamsync::detail {
namespace {
std::int64_t nanoseconds(double seconds) {
    const long double value = static_cast<long double>(seconds) * 1e9L;
    if (!std::isfinite(value) || value < std::numeric_limits<std::int64_t>::min() ||
        value > std::numeric_limits<std::int64_t>::max())
        throw std::runtime_error("invalid chrony time value");
    return static_cast<std::int64_t>(std::round(value));
}
double numeric(const std::string& field) {
    std::istringstream stream(field);
    stream.imbue(std::locale::classic());
    double value{};
    if (!(stream >> value) || !std::isfinite(value))
        throw std::runtime_error("invalid chrony tracking value");
    stream >> std::ws;
    if (!stream.eof())
        throw std::runtime_error("invalid chrony tracking value");
    return value;
}
void startup_check(const std::atomic<bool>& stopping, std::int64_t deadline) {
    if (stopping)
        throw std::runtime_error("node platform startup cancelled");
    if (monotonic_ns() >= deadline)
        throw std::runtime_error("node platform startup deadline exceeded");
}
} // namespace

std::string clock_config(const SyncConfig& config, const std::string& local,
                         const std::string& directory) {
    std::ostringstream out;
    out << "pidfile " << directory << "/chronyd.pid\n"
        << "bindcmdaddress " << directory << "/chronyd.sock\ncmdport 0\n";
    if (local == config.reference_node()) {
        out << "local stratum 8\nport " << config.clock_port << "\nbindaddress "
            << config.node(local).address << '\n';
        for (const auto& node : config.nodes)
            if (node.id != local)
                out << "allow " << node.address << "/32\n";
    } else {
        out << "port 0\nbindacqaddress " << config.node(local).address << "\nserver " << config.node(config.reference_node()).address
            << " port " << config.clock_port << " iburst minpoll 0 maxpoll 0\n";
    }
    return out.str();
}

ClockEstimate tracking_estimate(const SyncConfig& config, const std::string& local,
                                const std::string& csv, std::int64_t realtime,
                                std::int64_t boottime, std::int64_t mapping_uncertainty) {
    ClockEstimate result;
    try {
        if (mapping_uncertainty < 0)
            throw std::runtime_error("negative clock mapping uncertainty");
        std::vector<std::string> fields;
        std::istringstream stream(csv);
        std::string field;
        while (std::getline(stream, field, ','))
            fields.push_back(field);
        if (fields.size() != 14)
            throw std::runtime_error("invalid chrony tracking field count");
        fields.back().erase(fields.back().find_last_not_of(" \r\n\t") + 1);
        if (fields[13] != "Normal" || numeric(fields[2]) <= 0)
            throw std::runtime_error("group clock is not synchronized");
        const bool reference = local == config.reference_node();
        std::string expected = "7F7F0101";
        if (!reference) {
            in_addr address{};
            if (inet_pton(AF_INET, config.node(config.reference_node()).address.c_str(), &address) != 1)
                throw std::runtime_error("invalid resolved reference address");
            std::ostringstream value;
            value << std::uppercase << std::hex << std::setw(8) << std::setfill('0')
                  << ntohl(address.s_addr);
            expected = value.str();
        }
        if (fields[0] != expected)
            throw std::runtime_error("private clock selected an unexpected reference");
        const double correction = numeric(fields[4]);
        // Chrony's positive CSV current_correction means the system clock is slow.
        const long double offset = static_cast<long double>(realtime) - boottime +
                                   static_cast<long double>(correction) * 1e9L;
        if (offset < std::numeric_limits<std::int64_t>::min() ||
            offset > std::numeric_limits<std::int64_t>::max())
            throw std::runtime_error("clock mapping exceeds timestamp range");
        result.offset_ns = static_cast<std::int64_t>(std::round(offset));
        if (reference) {
            result.uncertainty_ns = mapping_uncertainty;
        } else {
            const double age = static_cast<double>(realtime) / 1e9 + correction - numeric(fields[3]);
            if (age < -1 || age > config.clock_max_age_seconds)
                throw std::runtime_error("private clock reference sample is stale");
            const double delay = numeric(fields[10]), dispersion = numeric(fields[11]);
            if (delay < 0 || dispersion < 0)
                throw std::runtime_error("negative clock uncertainty");
            const double residual = std::max({std::abs(numeric(fields[5])),
                                             std::abs(numeric(fields[6])), delay / 2 + dispersion});
            result.uncertainty_ns = nanoseconds(residual + static_cast<double>(mapping_uncertainty) / 1e9);
        }
        if (result.uncertainty_ns > nanoseconds(config.clock_max_offset_ms / 1000))
            throw std::runtime_error("private clock uncertainty exceeds configured threshold");
        result.valid = true;
    } catch (const std::exception& error) {
        result.fault = error.what();
    }
    return result;
}

class NetworkLink::Impl {
  public:
    std::string interface, connection_id, address;
#ifdef __linux__
    sd_bus* bus = nullptr;
    std::string activation;
    static void dbus_check(int code) {
        if (code < 0)
            throw std::runtime_error(std::string("NetworkManager D-Bus: ") + std::strerror(-code));
    }
    void activate(std::int64_t deadline) {
        dbus_check(sd_bus_open_system(&bus));
        const auto remaining = static_cast<std::uint64_t>(std::max<std::int64_t>(1, (deadline - monotonic_ns()) / 1000));
        dbus_check(sd_bus_set_method_call_timeout(bus, remaining));
        sd_bus_message* reply = nullptr;
        const int found = sd_bus_call_method(bus, "org.freedesktop.NetworkManager",
            "/org/freedesktop/NetworkManager", "org.freedesktop.NetworkManager",
            "GetDeviceByIpIface", nullptr, &reply, "s", interface.c_str());
        std::unique_ptr<sd_bus_message, decltype(&sd_bus_message_unref)> device_reply(reply, &sd_bus_message_unref);
        dbus_check(found);
        const char* device_path = nullptr;
        dbus_check(sd_bus_message_read(device_reply.get(), "o", &device_path));
        const std::string device(device_path);
        device_reply.reset();
        reply = nullptr;
        sd_bus_message* request = nullptr;
        dbus_check(sd_bus_message_new_method_call(bus, &request, "org.freedesktop.NetworkManager",
            "/org/freedesktop/NetworkManager", "org.freedesktop.NetworkManager",
            "AddAndActivateConnection2"));
        const auto setting = [&](const char* name) {
            dbus_check(sd_bus_message_open_container(request, 'e', "sa{sv}"));
            dbus_check(sd_bus_message_append(request, "s", name));
            dbus_check(sd_bus_message_open_container(request, 'a', "{sv}"));
        };
        const auto value = [&](const char* key, const char* text) {
            dbus_check(sd_bus_message_open_container(request, 'e', "sv"));
            dbus_check(sd_bus_message_append(request, "s", key));
            dbus_check(sd_bus_message_open_container(request, 'v', "s"));
            dbus_check(sd_bus_message_append(request, "s", text));
            dbus_check(sd_bus_message_close_container(request));
            dbus_check(sd_bus_message_close_container(request));
        };
        const auto end_setting = [&] {
            dbus_check(sd_bus_message_close_container(request));
            dbus_check(sd_bus_message_close_container(request));
        };
        try {
            dbus_check(sd_bus_message_open_container(request, 'a', "{sa{sv}}"));
            setting("connection");
            value("id", connection_id.c_str());
            value("type", "802-3-ethernet");
            value("interface-name", interface.c_str());
            dbus_check(sd_bus_message_append(request, "{sv}", "autoconnect", "b", 0));
            end_setting();
            setting("ipv4");
            value("method", "link-local");
            dbus_check(sd_bus_message_append(request, "{sv}", "never-default", "b", 1));
            end_setting();
            setting("ipv6");
            value("method", "disabled");
            end_setting();
            dbus_check(sd_bus_message_close_container(request));
            dbus_check(sd_bus_message_append(request, "oo", device.c_str(), "/"));
            dbus_check(sd_bus_message_open_container(request, 'a', "{sv}"));
            value("persist", "volatile");
            value("bind-activation", "dbus-client");
            dbus_check(sd_bus_message_close_container(request));
            reply = nullptr;
            const int activated = sd_bus_call(bus, request, remaining, nullptr, &reply);
            sd_bus_message_unref(request);
            request = nullptr;
            dbus_check(activated);
            const char *profile = nullptr, *active = nullptr;
            dbus_check(sd_bus_message_read(reply, "oo", &profile, &active));
            activation = active;
            sd_bus_message_unref(reply);
        } catch (...) {
            sd_bus_message_unref(request);
            sd_bus_message_unref(reply);
            throw;
        }
    }
    std::string link_address() const {
        ifaddrs* interfaces = nullptr;
        if (getifaddrs(&interfaces))
            throw std::runtime_error("cannot inspect synchronization interface");
        std::string result;
        for (auto entry = interfaces; entry; entry = entry->ifa_next) {
            if (!entry->ifa_addr || entry->ifa_addr->sa_family != AF_INET ||
                interface != entry->ifa_name)
                continue;
            const auto* ipv4 = reinterpret_cast<const sockaddr_in*>(entry->ifa_addr);
            if ((ntohl(ipv4->sin_addr.s_addr) & 0xffff0000U) != 0xa9fe0000U)
                continue;
            char text[INET_ADDRSTRLEN];
            if (inet_ntop(AF_INET, &ipv4->sin_addr, text, sizeof(text)))
                result = text;
        }
        freeifaddrs(interfaces);
        return result;
    }
#endif
    Impl(std::string device, std::string id)
        : interface(std::move(device)), connection_id(std::move(id)) {
        if (interface.empty() || connection_id.empty())
            throw std::invalid_argument("network interface and connection ID must not be empty");
    }
};

NetworkLink::NetworkLink(std::string interface, std::string connection_id)
    : impl_(std::make_unique<Impl>(std::move(interface), std::move(connection_id))) {}
NetworkLink::~NetworkLink() { stop(); }

std::string NetworkLink::start(const std::atomic<bool>& stopping, std::int64_t deadline_ns) {
    if (!impl_->address.empty())
        throw std::runtime_error("network link already started");
    startup_check(stopping, deadline_ns);
#ifdef __linux__
    try {
        impl_->activate(deadline_ns);
        auto next_progress = std::int64_t{0};
        while ((impl_->address = impl_->link_address()).empty()) {
            startup_check(stopping, deadline_ns);
            if (monotonic_ns() >= next_progress) {
                std::cerr << "Waiting for link-local IPv4 address on " << impl_->interface << '\n';
                next_progress = monotonic_ns() + 1000000000LL;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        return impl_->address;
    } catch (...) {
        stop();
        throw;
    }
#else
    throw std::runtime_error("transient networking requires Linux and NetworkManager");
#endif
}

void NetworkLink::stop() {
#ifdef __linux__
    if (impl_->bus) {
        if (!impl_->activation.empty()) {
            // Closing this bus also disconnects the lifetime-bound activation on crash.
            sd_bus_set_method_call_timeout(impl_->bus, 1000000);
            sd_bus_call_method(impl_->bus, "org.freedesktop.NetworkManager",
                "/org/freedesktop/NetworkManager", "org.freedesktop.NetworkManager",
                "DeactivateConnection", nullptr, nullptr, "o", impl_->activation.c_str());
        }
        sd_bus_flush_close_unref(impl_->bus);
        impl_->bus = nullptr;
        impl_->activation.clear();
    }
#endif
    impl_->address.clear();
}

class NodePlatform::Impl {
  public:
    SyncConfig config;
    std::string local, directory;
    bool started = false;
#ifdef __linux__
    std::unique_ptr<NetworkLink> link;
    pid_t child = -1;
    int discovery = -1;
    std::atomic<bool> discovery_stopping{false};
    std::thread discovery_thread;
    std::mutex mutex;
    std::condition_variable changed;
    std::map<std::string, std::string> addresses;
    std::string discovery_fault;

    void discover(const std::string& address) {
        const unsigned interface = if_nametoindex(config.node(local).interface.c_str());
        if (!interface)
            throw std::runtime_error("unknown synchronization interface");
        discovery = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
        if (discovery < 0)
            throw std::runtime_error("cannot open discovery socket");
        const auto option = [&](int level, int name, const void* data, socklen_t size) {
            if (setsockopt(discovery, level, name, data, size))
                throw std::runtime_error("cannot configure interface-scoped discovery");
        };
        const int yes = 1, no = 0;
        option(SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
        option(IPPROTO_IP, IP_PKTINFO, &yes, sizeof(yes));
        option(IPPROTO_IP, IP_MULTICAST_ALL, &no, sizeof(no));
        ip_mreqn membership{};
        inet_pton(AF_INET, "239.255.58.80", &membership.imr_multiaddr);
        inet_pton(AF_INET, address.c_str(), &membership.imr_address);
        membership.imr_ifindex = static_cast<int>(interface);
        option(IPPROTO_IP, IP_ADD_MEMBERSHIP, &membership, sizeof(membership));
        option(IPPROTO_IP, IP_MULTICAST_IF, &membership, sizeof(membership));
        const unsigned char ttl = 1;
        option(IPPROTO_IP, IP_MULTICAST_TTL, &ttl, sizeof(ttl));
        sockaddr_in endpoint{};
        endpoint.sin_family = AF_INET;
        endpoint.sin_port = htons(58079);
        endpoint.sin_addr.s_addr = htonl(INADDR_ANY);
        if (bind(discovery, reinterpret_cast<sockaddr*>(&endpoint), sizeof(endpoint)))
            throw std::runtime_error("cannot bind discovery socket");
        endpoint.sin_addr = membership.imr_multiaddr;
        addresses[local] = address;
        discovery_stopping = false;
        discovery_thread = std::thread([this, interface, endpoint, discovery_config = config] {
            try {
                const auto contract = nlohmann::json::parse(discovery_config.sync_json());
                const std::string announcement = nlohmann::json{
                    {"group", discovery_config.group_id}, {"node", local}, {"sync", contract}}.dump();
                auto next_announce = std::int64_t{0};
                while (!discovery_stopping) {
                    if (monotonic_ns() >= next_announce) {
                        if (sendto(discovery, announcement.data(), announcement.size(), 0,
                                   reinterpret_cast<const sockaddr*>(&endpoint), sizeof(endpoint)) < 0)
                            throw std::runtime_error("cannot announce synchronization node");
                        next_announce = monotonic_ns() + 250000000LL;
                    }
                    pollfd readable{discovery, POLLIN, 0};
                    if (poll(&readable, 1, 100) <= 0)
                        continue;
                    char data[65536];
                    alignas(cmsghdr) char ancillary[CMSG_SPACE(sizeof(in_pktinfo))];
                    sockaddr_in source{};
                    iovec buffer{data, sizeof(data)};
                    msghdr message{};
                    message.msg_name = &source;
                    message.msg_namelen = sizeof(source);
                    message.msg_iov = &buffer;
                    message.msg_iovlen = 1;
                    message.msg_control = ancillary;
                    message.msg_controllen = sizeof(ancillary);
                    const auto count = recvmsg(discovery, &message, 0);
                    if (count < 0 || message.msg_flags & MSG_TRUNC)
                        continue;
                    bool matching_interface = false;
                    for (auto control = CMSG_FIRSTHDR(&message); control;
                         control = CMSG_NXTHDR(&message, control)) {
                        if (control->cmsg_level == IPPROTO_IP && control->cmsg_type == IP_PKTINFO)
                            matching_interface = reinterpret_cast<in_pktinfo*>(CMSG_DATA(control))->ipi_ifindex ==
                                                 static_cast<int>(interface);
                    }
                    if (!matching_interface)
                        continue;
                    const auto peer = nlohmann::json::parse(data, data + count, nullptr, false);
                    if (!peer.is_object() || peer.value("group", "") != discovery_config.group_id)
                        continue;
                    const auto id = peer.value("node", "");
                    if (std::none_of(discovery_config.nodes.begin(), discovery_config.nodes.end(),
                                     [&](const NodeConfig& node) { return node.id == id; }))
                        continue;
                    if (peer.value("sync", nlohmann::json{}) != contract)
                        throw std::runtime_error("discovered node synchronization configuration differs: " + id);
                    char text[INET_ADDRSTRLEN];
                    inet_ntop(AF_INET, &source.sin_addr, text, sizeof(text));
                    std::lock_guard<std::mutex> lock(mutex);
                    const auto existing = addresses.find(id);
                    if (existing != addresses.end() && existing->second != text)
                        throw std::runtime_error("multiple addresses claim synchronization node: " + id);
                    addresses[id] = text;
                    changed.notify_all();
                }
            } catch (const std::exception& error) {
                std::lock_guard<std::mutex> lock(mutex);
                discovery_fault = error.what();
                changed.notify_all();
            }
        });
    }
    void launch_clock() {
        char pattern[] = "/tmp/bcamsync-XXXXXX";
        const char* path = mkdtemp(pattern);
        if (!path)
            throw std::runtime_error("cannot create private clock directory");
        directory = path;
        const std::string file = directory + "/chrony.conf";
        std::ofstream output(file);
        output << clock_config(config, local, directory);
        output.close();
        if (!output)
            throw std::runtime_error("cannot write private clock configuration");
        const std::string log = directory + "/chronyd.log";
        const int log_fd = open(log.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
        if (log_fd < 0)
            throw std::runtime_error("cannot open private clock log");
        const pid_t parent = getpid();
        const passwd* account = getpwuid(geteuid());
        if (!account) {
            close(log_fd);
            throw std::runtime_error("cannot identify private clock process account");
        }
        const std::string user = account->pw_name;
        child = fork();
        if (child == 0) {
            if (prctl(PR_SET_PDEATHSIG, SIGKILL) || getppid() != parent)
                _exit(127);
            dup2(log_fd, STDOUT_FILENO);
            dup2(log_fd, STDERR_FILENO);
            close(log_fd);
            execlp("chronyd", "chronyd", "-n", "-x", "-U", "-4", "-u", user.c_str(), "-f", file.c_str(),
                   static_cast<char*>(nullptr));
            _exit(127);
        }
        close(log_fd);
        if (child < 0)
            throw std::runtime_error("cannot start private chronyd");
    }
#endif
    Impl(SyncConfig value, std::string node) : config(std::move(value)), local(std::move(node)) {}
};

NodePlatform::NodePlatform(SyncConfig config, std::string local)
    : impl_(std::make_unique<Impl>(std::move(config), std::move(local))) {}
NodePlatform::~NodePlatform() { stop(); }

void NodePlatform::start(SyncConfig& resolved, const std::atomic<bool>& stopping,
                         std::int64_t deadline_ns) {
    if (impl_->started)
        throw std::runtime_error("node platform already started");
    impl_->config = resolved;
    startup_check(stopping, deadline_ns);
    if (resolved.nodes.size() == 1 || resolved.clock_mode == "host") {
        impl_->started = true;
        return;
    }
#ifdef __linux__
    try {
        if (resolved.network_mode == "auto") {
            std::cerr << "Activating transient synchronization link " << resolved.node(impl_->local).interface << '\n';
            impl_->link = std::make_unique<NetworkLink>(resolved.node(impl_->local).interface,
                "bcamsync-" + resolved.group_id + "-" + impl_->local);
            const auto address = impl_->link->start(stopping, deadline_ns);
            auto next_progress = std::int64_t{0};
            impl_->discover(address);
            std::unique_lock<std::mutex> lock(impl_->mutex);
            next_progress = 0;
            while (impl_->addresses.size() != resolved.nodes.size()) {
                startup_check(stopping, deadline_ns);
                if (!impl_->discovery_fault.empty())
                    throw std::runtime_error(impl_->discovery_fault);
                if (monotonic_ns() >= next_progress) {
                    std::cerr << "Discovering group nodes: " << impl_->addresses.size() << '/' << resolved.nodes.size() << '\n';
                    next_progress = monotonic_ns() + 1000000000LL;
                }
                impl_->changed.wait_for(lock, std::chrono::milliseconds(100));
            }
            for (auto& node : resolved.nodes)
                node.address = impl_->addresses.at(node.id);
            impl_->config = resolved;
        }
        std::cerr << "Starting private clock estimator for " << impl_->local << '\n';
        impl_->launch_clock();
        impl_->started = true;
        std::string fault;
        auto next_progress = std::int64_t{0};
        while (true) {
            startup_check(stopping, deadline_ns);
            const auto estimate = sample();
            if (estimate.valid)
                break;
            fault = estimate.fault;
            int status{};
            if (waitpid(impl_->child, &status, WNOHANG) == impl_->child) {
                impl_->child = -1;
                std::ifstream log(impl_->directory + "/chronyd.log");
                const std::string output((std::istreambuf_iterator<char>(log)), {});
                throw std::runtime_error("private chronyd exited: " + output);
            }
            if (monotonic_ns() >= next_progress) {
                std::cerr << "Waiting for group clock: " << fault << '\n';
                next_progress = monotonic_ns() + 1000000000LL;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
    } catch (...) {
        stop();
        throw;
    }
#else
    throw std::runtime_error("physical multi-node platform requires Linux, NetworkManager and chrony");
#endif
}

ClockEstimate NodePlatform::sample() {
    if (!impl_->started)
        return {false, 0, 0, "node platform is not started"};
    if (impl_->config.nodes.size() == 1)
        return {true, 0, 0, {}};
    try {
        std::string report;
        if (impl_->config.clock_mode != "host") {
#ifdef __linux__
            {
                std::lock_guard<std::mutex> lock(impl_->mutex);
                if (!impl_->discovery_fault.empty())
                    throw std::runtime_error(impl_->discovery_fault);
            }
            report = run_program({"chronyc", "-c", "-h", impl_->directory + "/chronyd.sock", "tracking"},
                                 impl_->config.request_timeout_seconds);
#endif
        }
        const auto before = boot_time_ns();
        const auto realtime = realtime_ns();
        const auto after = boot_time_ns();
        const auto boottime = before + (after - before) / 2;
        if (impl_->config.clock_mode == "host")
            return {true, realtime - boottime, (after - before) / 2, {}};
        return tracking_estimate(impl_->config, impl_->local, report, realtime, boottime,
                                 (after - before) / 2);
    } catch (const std::exception& error) {
        return {false, 0, 0, error.what()};
    }
}

void NodePlatform::stop() {
#ifdef __linux__
    impl_->discovery_stopping = true;
    if (impl_->discovery_thread.joinable())
        impl_->discovery_thread.join();
    if (impl_->discovery >= 0) {
        close(impl_->discovery);
        impl_->discovery = -1;
    }
    impl_->addresses.clear();
    impl_->discovery_fault.clear();
    if (impl_->child > 0) {
        kill(impl_->child, SIGKILL);
        while (waitpid(impl_->child, nullptr, 0) < 0 && errno == EINTR) {}
        impl_->child = -1;
    }
    impl_->link.reset();
#endif
    if (!impl_->directory.empty()) {
        std::error_code error;
        std::filesystem::remove_all(impl_->directory, error);
        impl_->directory.clear();
    }
    impl_->started = false;
}
} // namespace bcamsync::detail
