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
    void start();
    bool wait_ready(std::chrono::milliseconds timeout);
    GroupStatus status() const;
    ClockMapping clock() const;
    void stop();
private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace bcamsync
