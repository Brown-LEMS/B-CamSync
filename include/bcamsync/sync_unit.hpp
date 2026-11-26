#pragma once
#include "sync_types.hpp"
#include <algorithm>
#include <map>
#include <memory>

namespace bcamsync {
/// Device adapter contract called by SyncSession, concurrently with capture callbacks.
/// Applications pass an adapter to the session; the library requests corrections.
class SyncUnit {
public:
    virtual ~SyncUnit() = default;
    /// Bind a fresh control round without starting or stopping acquisition.
    virtual void bind(const std::string& session_id, std::uint64_t round_id) = 0;
    /// Preserve sensor_timestamp_ns; frame timestamp_ns and restoration boundaries use local boot time.
    virtual TimingSnapshot snapshot() const = 0;
    /// Inclusive suffix, retaining the cursor frame to verify capture continuity.
    virtual TimingSnapshot snapshot_from(std::uint64_t sequence) const {
        auto result = snapshot();
        auto cursor = std::find_if(result.frames.begin(), result.frames.end(),
                                   [sequence](const auto& f) { return f.camera_sequence == sequence; });
        if (cursor != result.frames.end())
            result.frames.erase(result.frames.begin(), cursor);
        return result;
    }
    /// Accept a library-generated correction; execution is confirmed by snapshot().correction.
    virtual CorrectionStatus adjust(const Pulse& pulse) = 0;
    /// Request baseline restoration; snapshot() must report its observed completion.
    virtual void cancel() = 0;
    virtual std::string fault() const = 0;
};
using LocalSyncUnits = std::map<std::string, std::shared_ptr<SyncUnit>>;

} // namespace bcamsync
