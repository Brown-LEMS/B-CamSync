#include "bcamsync/phase.hpp"
#include "bcamsync/actuator.hpp"
#include "bcamsync/detail/rust_ffi.h"
#include <cmath>
#include <nlohmann/json.hpp>
#include <memory>
#include <utility>

namespace bcamsync {
namespace {
using Json = nlohmann::json;
Json response(char* raw) {
    if (!raw) throw std::runtime_error("Rust core returned no response");
    std::unique_ptr<char, decltype(&bcamsync_rust_string_free)> value(raw, bcamsync_rust_string_free);
    const auto j = Json::parse(value.get());
    if (j.contains("error")) {
        const auto message = j.at("error").get<std::string>();
        switch (j.value("kind", 2)) {
        case 1: throw std::invalid_argument(message);
        case 3: throw WindowPending(message);
        default: throw TimingError(message);
        }
    }
    return j.at("ok");
}
Json policy_json(const PhasePolicy& p) {
    Json j;
#define FIELD(name) j[#name] = p.name
    FIELD(window_seconds); FIELD(evaluation_seconds); FIELD(freshness_seconds);
    FIELD(deadband_ms); FIELD(ready_p95_ms); FIELD(ready_max_ms);
    FIELD(ready_windows); FIELD(startup_delta_us); FIELD(startup_frames);
    FIELD(tracking_delta_us); FIELD(tracking_frames); FIELD(correction_interval_seconds);
    FIELD(settling_seconds); FIELD(application_delay_seconds); FIELD(startup_max_corrections);
    FIELD(excursion_ms); FIELD(excursion_timeout_seconds); FIELD(no_progress_corrections);
    FIELD(progress_ms); FIELD(gain); FIELD(max_mapping_span_ns); FIELD(max_samples);
#undef FIELD
    return j;
}
Json snapshot_json(const TimingSnapshot& s) {
    Json frames = Json::array();
    for (const auto& f : s.frames)
        frames.push_back({{"sequence", f.camera_sequence}, {"sensor", f.sensor_timestamp_ns},
            {"time", f.timestamp_ns}, {"span", f.mapping_span_ns},
            {"requested", f.requested_duration_us}, {"reported", f.reported_duration_us},
            {"correction_id", f.correction_id}});
    return {{"unit", s.unit_id}, {"boundary", s.snapshot_timestamp_ns}, {"frames", frames},
        {"correction", {{"state", s.correction.state}, {"id", s.correction.correction_id},
                        {"restored", s.correction.restored_timestamp_ns}}}};
}
Measurement measurement(const Json& j) {
    Measurement m;
#define FIELD(name) j.at(#name).get_to(m.name)
    FIELD(window_start_ns); FIELD(window_end_ns); FIELD(pair_count); FIELD(master_frames);
    FIELD(slave_frames); FIELD(unmatched_master); FIELD(unmatched_slave); FIELD(phase_ns);
    FIELD(absolute_p95_ns); FIELD(absolute_max_ns); FIELD(master_period_ns); FIELD(slave_period_ns);
    FIELD(drift_ns_per_second); FIELD(predicted_phase_ns); FIELD(ready_windows); FIELD(excursion);
#undef FIELD
    if (!j.at("correction_progress_ns").is_null()) m.correction_progress_ns = j.at("correction_progress_ns").get<double>();
    return m;
}
PhaseActuator::State state(const Json& j) {
    PhaseActuator::State s;
#define FIELD(name) j.at(#name).get_to(s.name)
    FIELD(session_id); FIELD(state); FIELD(round_id); FIELD(correction_id); FIELD(delta_us);
    FIELD(count); FIELD(submitted_frames); FIELD(expires_ns); FIELD(deadline_mono_ns);
    FIELD(restored_timestamp_ns); FIELD(restore_sequence); FIELD(cancelled);
#undef FIELD
    return s;
}
Json phase_call(void** h, const char* op, const Json& input) {
    return response(bcamsync_phase_call(h, op, input.dump().c_str()));
}
PhaseActuator::State actuator_call(void* h, const char* op, const Json& input = Json::object()) {
    return state(response(bcamsync_actuator_call(h, op, input.dump().c_str())));
}
void check_actuator(int result) { if (result) throw std::runtime_error("Rust actuator call failed"); }
} // namespace
void PhasePolicy::validate() const { phase_call(nullptr, "validate_policy", {{"policy", policy_json(*this)}}); }
void validate_timing(const TimingSnapshot& s, std::int64_t now, const PhasePolicy& p) {
    phase_call(nullptr, "validate_timing", {{"snapshot", snapshot_json(s)}, {"now", now}, {"policy", policy_json(p)}});
}
Measurement estimate_phase(const TimingSnapshot& master, const TimingSnapshot& slave,
                          std::int64_t now, const PhasePolicy& p, std::int64_t after,
                          std::optional<double> previous, std::int64_t nominal) {
    if (previous && !std::isfinite(*previous))
        throw std::invalid_argument("invalid phase estimation arguments");
    return measurement(phase_call(nullptr, "estimate", {{"master", snapshot_json(master)},
        {"slave", snapshot_json(slave)}, {"now", now}, {"policy", policy_json(p)},
        {"after", after}, {"previous", previous ? Json(*previous) : Json(nullptr)}, {"nominal", nominal}}));
}
PhaseController::PhaseController(PhasePolicy p, std::int64_t nominal) {
    phase_call(&handle_, "new", {{"policy", policy_json(p)}, {"nominal", nominal}});
}
PhaseController::~PhaseController() { bcamsync_phase_free(handle_); }
PhaseController::PhaseController(const PhaseController& other) {
    phase_call(&handle_, "clone", {{"source", reinterpret_cast<std::uintptr_t>(other.handle_)}});
}
PhaseController& PhaseController::operator=(const PhaseController& other) {
    if (this != &other) { PhaseController copy(other); std::swap(handle_, copy.handle_); }
    return *this;
}
PhaseController::PhaseController(PhaseController&& other) noexcept : handle_(std::exchange(other.handle_, nullptr)) {}
PhaseController& PhaseController::operator=(PhaseController&& other) noexcept {
    if (this != &other) { bcamsync_phase_free(handle_); handle_ = std::exchange(other.handle_, nullptr); }
    return *this;
}
void PhaseController::reset(std::optional<std::int64_t> mono, std::int64_t after) {
    phase_call(&handle_, "reset", {{"mono", mono ? Json(*mono) : Json(nullptr)}, {"after", after}});
}
bool PhaseController::ready() const {
    auto h = handle_;
    return phase_call(&h, "ready", Json::object()).get<bool>();
}
PhaseDecision PhaseController::tick(const TimingSnapshot& master, const TimingSnapshot& slave,
                                    std::int64_t now, std::int64_t mono, bool clock_valid) {
    const auto j = phase_call(&handle_, "tick", {{"master", snapshot_json(master)},
        {"slave", snapshot_json(slave)}, {"now", now}, {"mono", mono}, {"clock_valid", clock_valid}});
    PhaseDecision d;
    d.state = j.at("state"); d.ready = j.at("ready"); d.fault = j.at("fault");
    if (!j.at("measurement").is_null()) d.measurement = measurement(j.at("measurement"));
    if (!j.at("action").is_null()) {
        const auto& a = j.at("action");
        d.action = Pulse{a.at("correction_id"), a.at("delta_us"), a.at("count"), a.at("expires_ns")};
    }
    return d;
}
PhaseActuator::PhaseActuator() : handle_(bcamsync_actuator_new()) {
    if (!handle_) throw std::bad_alloc();
}
PhaseActuator::~PhaseActuator() { bcamsync_actuator_free(handle_); }
PhaseActuator::State PhaseActuator::snapshot() const { return actuator_call(handle_, "snapshot"); }
PhaseActuator::State PhaseActuator::bind(const std::string& session, uint64_t round) {
    return actuator_call(handle_, "bind", {{"session", session}, {"round", round}});
}
PhaseActuator::State PhaseActuator::query(const std::string& session, uint64_t round, uint64_t id) const {
    return actuator_call(handle_, "query", {{"session", session}, {"round", round}, {"id", id}});
}
PhaseActuator::State PhaseActuator::adjust(const std::string& session, uint64_t round, uint64_t id,
    int delta, int count, int64_t expires, int64_t now, int64_t mono, int64_t baseline,
    int64_t minimum, int64_t maximum) {
    return actuator_call(handle_, "adjust", {{"session", session}, {"round", round}, {"id", id},
        {"delta", delta}, {"count", count}, {"expires", expires}, {"now", now}, {"mono", mono},
        {"baseline", baseline}, {"minimum", minimum}, {"maximum", maximum}});
}
PhaseActuator::State PhaseActuator::cancel(const std::string& session, uint64_t round) {
    return actuator_call(handle_, "cancel", {{"session", session}, {"round", round}});
}
void PhaseActuator::cancel_active() { check_actuator(bcamsync_actuator_cancel_active(handle_)); }
PhaseActuator::Submission PhaseActuator::next(int64_t baseline, int64_t mono) {
    BcamsyncSubmission s{}; check_actuator(bcamsync_actuator_next(handle_, baseline, mono, &s));
    return {s.duration_us, s.correction_id, s.restoration, s.round_id};
}
void PhaseActuator::completed(const Submission& s, uint64_t sequence, int64_t timestamp) {
    check_actuator(bcamsync_actuator_completed(handle_, {s.duration_us, s.correction_id, s.restoration, s.round_id}, sequence, timestamp));
}
} // namespace bcamsync
