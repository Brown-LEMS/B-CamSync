#pragma once
#include "backend.hpp"
#include <chrono>
#include <memory>
#include <string>
namespace bcamsync {
namespace detail { class SessionTestAccess; }
struct UnitStatus {
    std::string unit_id, state = "warming", fault;
    bool ready = false;
    double phase_ms = 0, p95_ms = 0, max_ms = 0;
    /// Zero until a settled phase window has been measured.
    std::int64_t measurement_end_ns = 0;
};
struct GroupStatus {
    std::string state = "stopped", fault;
    bool ready = false;
    std::vector<UnitStatus> units;
};
/// A local acquisition session; coordinator placement follows reference_unit.
class Session {
  public:
    Session(Config config, std::string local_node);
    ~Session();
    Session(const Session&) = delete;
    Session& operator=(const Session&) = delete;
    void start();
    /// Optionally wait for synchronization; read() does not depend on readiness.
    bool wait_ready(std::chrono::milliseconds timeout);
    /// Return a local frame even while synchronization is starting or faulted.
    std::shared_ptr<const FramePacket> read(const std::string& unit_id,
                                            std::chrono::milliseconds timeout);
    GroupStatus status() const;
    void stop();

  private:
    friend class detail::SessionTestAccess;
    Session(Config config, std::string local_node,
            std::vector<std::unique_ptr<AcquisitionBackend>> backends);
    class Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace bcamsync
