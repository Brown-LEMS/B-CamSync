#pragma once
#include "bcamsync/sync_types.hpp"
#include <atomic>
#include <memory>

namespace bcamsync::detail {
/// Add offset_ns to a local CLOCK_BOOTTIME timestamp to obtain group time.
struct ClockEstimate {
    bool valid = false;
    std::int64_t offset_ns = 0, uncertainty_ns = 0;
    std::string fault;
};

std::string clock_config(const SyncConfig& config, const std::string& local,
                         const std::string& directory);
ClockEstimate tracking_estimate(const SyncConfig& config, const std::string& local,
                                const std::string& csv, std::int64_t realtime,
                                std::int64_t boottime, std::int64_t mapping_uncertainty = 0);

/// Reuse an active interface IPv4 or own a transient NetworkManager activation.
class NetworkLink {
  public:
    NetworkLink(std::string interface, std::string connection_id);
    ~NetworkLink();
    NetworkLink(const NetworkLink&) = delete;
    NetworkLink& operator=(const NetworkLink&) = delete;
    std::string start(const std::atomic<bool>& stopping, std::int64_t deadline_ns);
    void stop();
  private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};

/// Compose NetworkSession with a private, non-stepping chronyd for one synchronization run.
class NodePlatform {
  public:
    NodePlatform(SyncConfig config, std::string local);
    ~NodePlatform();
    NodePlatform(const NodePlatform&) = delete;
    NodePlatform& operator=(const NodePlatform&) = delete;
    void start(SyncConfig& resolved, const std::atomic<bool>& stopping, std::int64_t deadline_ns);
    ClockEstimate sample();
    void stop();

  private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace bcamsync::detail
