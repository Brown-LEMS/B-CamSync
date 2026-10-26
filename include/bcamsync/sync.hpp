#pragma once
#include "sync_unit.hpp"
#include <chrono>
#include <memory>

namespace bcamsync {
/// Attach ongoing synchronization to user-owned capture. stop() leaves capture running.
class SyncSession {
public:
    SyncSession(SyncConfig config, std::string local_node, LocalSyncUnits units);
    ~SyncSession();
    SyncSession(const SyncSession&) = delete;
    SyncSession& operator=(const SyncSession&) = delete;
    /// Join independently; peers may start later. A completed round can be started again.
    void start();
    /// Refresh all node IPv4 addresses while stopped, retaining the control namespace.
    void set_node_addresses(const std::map<std::string, std::string>& addresses);
    /// Wait only: timeout returns false without stopping; faults throw, group stop returns false.
    bool wait_ready(std::chrono::milliseconds timeout);
    GroupStatus status() const;
    ClockMapping clock() const;
    /// Request group shutdown and restore local controls, leaving acquisition running. Idempotent.
    void stop();
private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace bcamsync
