#pragma once
#include "sync_types.hpp"
#include <map>
#include <memory>

namespace bcamsync {
/// Adapt an already-running capture system; methods may run on the synchronization thread.
class SyncUnit {
public:
    virtual ~SyncUnit() = default;
    /// Bind a fresh control round without starting or stopping acquisition.
    virtual void bind(const std::string& session_id, std::uint64_t round_id) = 0;
    /// Immutable sensor identity; timestamps and restoration boundaries use local boot time.
    virtual TimingSnapshot snapshot() const = 0;
    /// Acceptance is separate from observed completion in snapshot().correction.
    virtual CorrectionStatus adjust(const Pulse& pulse) = 0;
    /// Request baseline restoration; snapshot() must report its observed completion.
    virtual void cancel() = 0;
    virtual std::string fault() const = 0;
};
using LocalSyncUnits = std::map<std::string, std::shared_ptr<SyncUnit>>;

} // namespace bcamsync
