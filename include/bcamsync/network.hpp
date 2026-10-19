#pragma once
#include "sync_types.hpp"
#include <atomic>
#include <chrono>
#include <memory>

namespace bcamsync {
namespace detail { class NodePlatform; class NetworkSessionTestAccess; }
/// Own a node's synchronization link and discovery independently of capture and clock estimation.
class NetworkSession {
public:
    NetworkSession(SyncConfig config, std::string local_node);
    ~NetworkSession();
    NetworkSession(const NetworkSession&) = delete;
    NetworkSession& operator=(const NetworkSession&) = delete;
    /// In auto mode reuse an active interface IPv4, or create a transient link, then announce.
    /// Peers need not have started; stop() preserves reused network connections.
    void start(std::chrono::milliseconds timeout = std::chrono::seconds(60));
    /// Resolve auto-mode peers, or retain explicit endpoints, and return an existing-network configuration.
    SyncConfig resolve(std::chrono::milliseconds timeout = std::chrono::seconds(60));
    /// Local IPv4 address while started; empty before start and after stop.
    std::string address() const;
    /// Background discovery failure; an absent peer is pending, not a fault.
    std::string fault() const;
    /// Cancel operations without releasing the local link; stop() completes cleanup.
    void request_stop();
    /// Cancel startup/resolution and release only this session's activation and discovery.
    void stop();
private:
    friend class detail::NodePlatform;
    friend class detail::NetworkSessionTestAccess;
    void start(const std::atomic<bool>& stopping, std::int64_t deadline_ns);
    SyncConfig resolve(const std::atomic<bool>& stopping, std::int64_t deadline_ns);
    SyncConfig resolve_available() const;
    void start_discovery(std::string address);
    class Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace bcamsync
