#pragma once
#include "types.hpp"
#include <chrono>
#include <memory>
#include <string>
namespace bcamsync {
/// SDK boundary: timing continues even when no application reads image packets.
/// Timing observations, packet timestamps, and pulse expiry use node-local CLOCK_BOOTTIME.
/// sensor_timestamp_ns retains the original SDK clock reading independently of that domain.
class AcquisitionBackend {
public:
    virtual ~AcquisitionBackend() = default;
    virtual void start(const std::string &session_id, std::uint64_t round_id) = 0;
    virtual void stop() = 0;
    virtual TimingSnapshot snapshot() const = 0;
    virtual CorrectionStatus adjust(const Pulse &pulse) = 0;
    virtual void cancel() = 0;
    virtual std::shared_ptr<const FramePacket> read(std::chrono::milliseconds timeout) = 0;
    virtual std::string fault() const = 0;
};
/// One node factory shares its SDK manager between all local units.
std::vector<std::unique_ptr<AcquisitionBackend>> make_arducam_backends(const std::vector<UnitConfig> &units, unsigned fps = 40);
/// Enumerate exact SDK camera IDs before creating a session; devices are not started.
std::vector<std::string> enumerate_arducam_cameras(const std::string &profile = "arducam_b0267_pi4");
}
