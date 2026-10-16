#pragma once
#include "bcamsync/sync_unit.hpp"
#include <chrono>

namespace bcamsync::detail {
/// Blocking shared runtime. Its caller owns the worker and any acquisition lifecycle.
class SyncRuntime {
public:
    SyncRuntime(SyncConfig config, std::string local_node, LocalSyncUnits units);
    ~SyncRuntime();
    void begin();
    void set_node_addresses(const std::map<std::string, std::string>& addresses);
    void run();
    void request_stop();
    bool stopping() const;
    void wait_for_stop();
    void finish();
    void fail(const std::string& reason);
    const std::string& identity() const;
    std::uint64_t round() const;
    bool wait_ready(std::chrono::milliseconds timeout);
    GroupStatus status() const;
    ClockMapping clock() const;
private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace bcamsync::detail
