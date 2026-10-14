#pragma once
#include "backend.hpp"
#include "sync_types.hpp"
#include <chrono>
#include <memory>
#include <string>
namespace bcamsync {
namespace detail { class SessionTestAccess; }
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
