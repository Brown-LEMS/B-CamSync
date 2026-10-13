#include "wire.hpp"
#include <arpa/inet.h>
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <poll.h>
#include <stdexcept>
#include <sys/socket.h>
#include <unistd.h>
namespace bcamsync::detail {
namespace {
constexpr std::size_t max_message = 1024 * 1024;
void wait_fd(int fd, short events, std::int64_t deadline) {
    while (true) {
        const auto remaining = deadline - monotonic_ns();
        if (remaining <= 0)
            throw std::runtime_error("control channel deadline exceeded");
        pollfd p{fd, events, 0};
        const int r = poll(&p, 1, static_cast<int>((remaining + 999999) / 1000000));
        if (r < 0 && errno == EINTR)
            continue;
        if (r < 0)
            throw std::runtime_error("control poll failed");
        if (r == 0)
            throw std::runtime_error("control channel deadline exceeded");
        if (p.revents & events)
            return;
        throw std::runtime_error("control channel disconnected");
    }
}
void nonblocking(int fd) {
    if (fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK) < 0)
        throw std::runtime_error("cannot set nonblocking socket");
    if (fcntl(fd, F_SETFD, FD_CLOEXEC) < 0)
        throw std::runtime_error("cannot set socket close-on-exec");
#ifdef SO_NOSIGPIPE
    int yes = 1;
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &yes, sizeof(yes));
#endif
}
sockaddr_in address(const NodeConfig& n) {
    sockaddr_in a{};
    a.sin_family = AF_INET;
    a.sin_port = htons(n.port);
    if (inet_pton(AF_INET, n.address.c_str(), &a.sin_addr) != 1)
        throw std::invalid_argument("numeric IPv4 address required");
    return a;
}
} // namespace
Connection::Connection(int fd, double timeout) : fd_(fd), timeout_(timeout) { nonblocking(fd); }
Connection::~Connection() { close(fd_); }
void Connection::shutdown() { ::shutdown(fd_, SHUT_RDWR); }
void Connection::send(const Json& j) {
    const auto data = j.dump() + "\n";
    if (data.size() > max_message)
        throw std::runtime_error("control message exceeds bound");
    std::size_t done = 0;
    const auto deadline = monotonic_ns() + static_cast<std::int64_t>(timeout_ * 1e9);
    while (done < data.size()) {
        wait_fd(fd_, POLLOUT, deadline);
#ifdef MSG_NOSIGNAL
        const int flags = MSG_NOSIGNAL;
#else
        const int flags = 0;
#endif
        const auto n = ::send(fd_, data.data() + done, data.size() - done, flags);
        if (n < 0 && (errno == EAGAIN || errno == EINTR))
            continue;
        if (n <= 0)
            throw std::runtime_error("control send failed");
        done += static_cast<std::size_t>(n);
    }
}
Json Connection::receive() {
    const auto deadline = monotonic_ns() + static_cast<std::int64_t>(timeout_ * 1e9);
    while (true) {
        const auto pos = pending_.find('\n');
        if (pos != std::string::npos) {
            auto line = pending_.substr(0, pos);
            pending_.erase(0, pos + 1);
            return Json::parse(line);
        }
        if (pending_.size() >= max_message)
            throw std::runtime_error("control message exceeds bound");
        wait_fd(fd_, POLLIN, deadline);
        char b[4096];
        auto n = recv(fd_, b, std::min(sizeof(b), max_message - pending_.size()), 0);
        if (n < 0 && (errno == EAGAIN || errno == EINTR))
            continue;
        if (n <= 0)
            throw std::runtime_error("control channel disconnected");
        pending_.append(b, static_cast<std::size_t>(n));
    }
}
int listener(const NodeConfig& n) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0)
        throw std::runtime_error("cannot create listener");
    try {
        nonblocking(fd);
        int yes = 1;
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
        auto a = address(n);
        if (bind(fd, reinterpret_cast<sockaddr*>(&a), sizeof(a)) || listen(fd, 1))
            throw std::runtime_error("cannot bind control endpoint " + n.address + ":" +
                                     std::to_string(n.port) + ": " + strerror(errno));
        return fd;
    } catch (...) {
        close(fd);
        throw;
    }
}
std::unique_ptr<Connection> connect(const NodeConfig& n, double timeout) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0)
        throw std::runtime_error("cannot create client");
    try {
        nonblocking(fd);
        auto a = address(n);
        int r = ::connect(fd, reinterpret_cast<sockaddr*>(&a), sizeof(a));
        if (r < 0 && errno != EINPROGRESS)
            throw std::runtime_error("cannot connect to " + n.id + ": " + strerror(errno));
        if (r < 0) {
            wait_fd(fd, POLLOUT, monotonic_ns() + static_cast<std::int64_t>(timeout * 1e9));
            int error = 0;
            socklen_t size = sizeof(error);
            if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &size) || error)
                throw std::runtime_error("cannot connect to " + n.id + ": " + strerror(error));
        }
        return std::make_unique<Connection>(fd, timeout);
    } catch (...) {
        close(fd);
        throw;
    }
}
Json snapshot_json(const TimingSnapshot& s) {
    Json rows = Json::array();
    for (const auto& f : s.frames)
        rows.push_back({{"sequence", f.camera_sequence},
                        {"sensor", f.sensor_timestamp_ns},
                        {"time", f.timestamp_ns},
                        {"span", f.mapping_span_ns},
                        {"requested", f.requested_duration_us},
                        {"reported", f.reported_duration_us},
                        {"correction_id", f.correction_id}});
    return {{"unit", s.unit_id},
            {"boundary", s.snapshot_timestamp_ns},
            {"frames", rows},
            {"correction",
             {{"state", s.correction.state},
              {"id", s.correction.correction_id},
              {"restored", s.correction.restored_timestamp_ns}}}};
}
TimingSnapshot snapshot_from_json(const Json& j) {
    TimingSnapshot s;
    s.unit_id = j.at("unit");
    s.snapshot_timestamp_ns = j.at("boundary");
    const auto& rows = j.at("frames");
    if (!rows.is_array() || rows.size() > 512)
        throw std::runtime_error("invalid timing window size");
    for (const auto& r : rows) {
        FrameTiming f;
        f.camera_sequence = r.at("sequence");
        f.sensor_timestamp_ns = r.at("sensor");
        f.timestamp_ns = r.at("time");
        f.mapping_span_ns = r.at("span");
        f.requested_duration_us = r.at("requested");
        f.reported_duration_us = r.at("reported");
        f.correction_id = r.at("correction_id");
        s.frames.push_back(f);
    }
    s.correction.state = j.at("correction").at("state");
    s.correction.correction_id = j.at("correction").at("id");
    s.correction.restored_timestamp_ns = j.at("correction").at("restored");
    return s;
}
Json topology_json(const Config& c) {
    Json ns = Json::array(), us = Json::array();
    for (const auto& n : c.nodes)
        ns.push_back({{"id", n.id}, {"address", n.address}, {"port", n.port}});
    for (const auto& u : c.units)
        us.push_back({{"id", u.id},
                      {"node_id", u.node_id},
                      {"camera_id", u.camera_id},
                      {"width", u.width},
                      {"height", u.height},
                      {"views", u.views},
                      {"fps", u.fps},
                      {"format", u.raw_format},
                      {"shift", u.r16_shift},
                      {"exposure", u.exposure_us},
                      {"gain", u.analogue_gain},
                      {"queue", u.queue_capacity},
                      {"pool", u.pool_capacity},
                      {"pipeline", u.pipeline_config}});
    return {
        {"group", c.group_id}, {"reference", c.reference_unit},   {"nodes", ns},
        {"units", us},         {"offset", c.clock_max_offset_ms}, {"age", c.clock_max_age_seconds}};
}
Json status_json(const GroupStatus& s) {
    Json us = Json::array();
    for (const auto& u : s.units)
        us.push_back({{"id", u.unit_id},
                      {"state", u.state},
                      {"ready", u.ready},
                      {"fault", u.fault},
                      {"phase", u.phase_ms},
                      {"p95", u.p95_ms},
                      {"max", u.max_ms},
                      {"measured_at", u.measurement_end_ns}});
    return {{"state", s.state}, {"ready", s.ready}, {"fault", s.fault}, {"units", us}};
}
GroupStatus status_from_json(const Json& j) {
    GroupStatus s;
    s.state = j.at("state");
    s.ready = j.at("ready");
    s.fault = j.at("fault");
    if (j.at("units").size() > 64)
        throw std::runtime_error("invalid group status size");
    for (const auto& v : j.at("units")) {
        UnitStatus u;
        u.unit_id = v.at("id");
        u.state = v.at("state");
        u.ready = v.at("ready");
        u.fault = v.at("fault");
        u.phase_ms = v.at("phase");
        u.p95_ms = v.at("p95");
        u.max_ms = v.at("max");
        u.measurement_end_ns = v.at("measured_at");
        s.units.push_back(u);
    }
    return s;
}
} // namespace bcamsync::detail
