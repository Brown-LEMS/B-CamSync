#pragma once
#include "bcamsync/types.hpp"
#include <atomic>
#include <memory>

namespace bcamsync::detail {
/// Add offset_ns to a local CLOCK_BOOTTIME timestamp to obtain group time.
struct ClockEstimate {
    bool valid = false;
    std::int64_t offset_ns = 0, uncertainty_ns = 0;
    std::string fault;
};

std::string clock_config(const Config& config, const std::string& local,
                         const std::string& directory);
ClockEstimate tracking_estimate(const Config& config, const std::string& local,
                                const std::string& csv, std::int64_t realtime,
                                std::int64_t boottime, std::int64_t mapping_uncertainty = 0);

/// Own only transient network activation and a private, non-stepping chronyd.
class NodePlatform {
  public:
    NodePlatform(Config config, std::string local);
    ~NodePlatform();
    NodePlatform(const NodePlatform&) = delete;
    NodePlatform& operator=(const NodePlatform&) = delete;
    void start(Config& resolved, const std::atomic<bool>& stopping, std::int64_t deadline_ns);
    ClockEstimate sample();
    void stop();

  private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace bcamsync::detail
