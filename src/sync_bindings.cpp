#include "sync_bindings.hpp"
#include "bcamsync/actuator.hpp"
#include "bcamsync/sync.hpp"
#include <cmath>
#include <filesystem>
#include <limits>
#include <pybind11/stl.h>
#include <pybind11/stl/filesystem.h>
#include <pybind11/trampoline_self_life_support.h>
#include <stdexcept>

namespace py = pybind11;
namespace bcamsync::python {
namespace {
class PythonSyncUnit : public SyncUnit, public py::trampoline_self_life_support {
public:
    using SyncUnit::SyncUnit;
    void bind(const std::string& session, std::uint64_t round) override {
        PYBIND11_OVERRIDE_PURE(void, SyncUnit, bind, session, round);
    }
    TimingSnapshot snapshot() const override {
        PYBIND11_OVERRIDE_PURE(TimingSnapshot, SyncUnit, snapshot,);
    }
    CorrectionStatus adjust(const Pulse& pulse) override {
        PYBIND11_OVERRIDE_PURE(CorrectionStatus, SyncUnit, adjust, pulse);
    }
    void cancel() override { PYBIND11_OVERRIDE_PURE(void, SyncUnit, cancel,); }
    std::string fault() const override { PYBIND11_OVERRIDE_PURE(std::string, SyncUnit, fault,); }
};
CorrectionStatus correction(const PhaseActuator::State& state) {
    return {state.state, state.correction_id, state.restored_timestamp_ns};
}
std::chrono::milliseconds timeout(double seconds) {
    if (!std::isfinite(seconds) || seconds < 0 ||
        seconds >= static_cast<double>(std::numeric_limits<std::int64_t>::max()) / 1000.0)
        throw std::invalid_argument("timeout_seconds must be a finite nonnegative duration");
    return std::chrono::milliseconds(static_cast<std::int64_t>(std::ceil(seconds * 1000)));
}
}

void bind_sync(py::module_& module) {
    module.def("boot_time_ns", &boot_time_ns);
    module.def("monotonic_ns", &monotonic_ns);
    py::class_<FrameTiming>(module, "FrameTiming")
        .def(py::init<>())
        .def_readwrite("camera_sequence", &FrameTiming::camera_sequence)
        .def_readwrite("sensor_timestamp_ns", &FrameTiming::sensor_timestamp_ns)
        .def_readwrite("timestamp_ns", &FrameTiming::timestamp_ns)
        .def_readwrite("mapping_span_ns", &FrameTiming::mapping_span_ns)
        .def_readwrite("requested_duration_us", &FrameTiming::requested_duration_us)
        .def_readwrite("reported_duration_us", &FrameTiming::reported_duration_us)
        .def_readwrite("correction_id", &FrameTiming::correction_id);
    py::class_<CorrectionStatus>(module, "CorrectionStatus")
        .def(py::init<>())
        .def_readwrite("state", &CorrectionStatus::state)
        .def_readwrite("correction_id", &CorrectionStatus::correction_id)
        .def_readwrite("restored_timestamp_ns", &CorrectionStatus::restored_timestamp_ns);
    py::class_<TimingSnapshot>(module, "TimingSnapshot")
        .def(py::init<>())
        .def_readwrite("unit_id", &TimingSnapshot::unit_id)
        .def_readwrite("snapshot_timestamp_ns", &TimingSnapshot::snapshot_timestamp_ns)
        .def_readwrite("frames", &TimingSnapshot::frames)
        .def_readwrite("correction", &TimingSnapshot::correction);
    py::class_<Pulse>(module, "Pulse")
        .def(py::init<>())
        .def_readwrite("correction_id", &Pulse::correction_id)
        .def_readwrite("delta_us", &Pulse::delta_us)
        .def_readwrite("count", &Pulse::count)
        .def_readwrite("expires_ns", &Pulse::expires_ns);
    py::class_<SyncUnitConfig>(module, "SyncUnitConfig")
        .def(py::init<>())
        .def_readwrite("id", &SyncUnitConfig::id)
        .def_readwrite("node_id", &SyncUnitConfig::node_id);
    py::class_<PhasePolicy>(module, "PhasePolicy")
        .def(py::init<>())
        .def_readwrite("window_seconds", &PhasePolicy::window_seconds)
        .def_readwrite("evaluation_seconds", &PhasePolicy::evaluation_seconds)
        .def_readwrite("freshness_seconds", &PhasePolicy::freshness_seconds)
        .def_readwrite("deadband_ms", &PhasePolicy::deadband_ms)
        .def_readwrite("ready_p95_ms", &PhasePolicy::ready_p95_ms)
        .def_readwrite("ready_max_ms", &PhasePolicy::ready_max_ms)
        .def_readwrite("ready_windows", &PhasePolicy::ready_windows)
        .def_readwrite("startup_delta_us", &PhasePolicy::startup_delta_us)
        .def_readwrite("startup_frames", &PhasePolicy::startup_frames)
        .def_readwrite("tracking_delta_us", &PhasePolicy::tracking_delta_us)
        .def_readwrite("tracking_frames", &PhasePolicy::tracking_frames)
        .def_readwrite("correction_interval_seconds", &PhasePolicy::correction_interval_seconds)
        .def_readwrite("settling_seconds", &PhasePolicy::settling_seconds)
        .def_readwrite("application_delay_seconds", &PhasePolicy::application_delay_seconds)
        .def_readwrite("startup_max_corrections", &PhasePolicy::startup_max_corrections)
        .def_readwrite("excursion_ms", &PhasePolicy::excursion_ms)
        .def_readwrite("excursion_timeout_seconds", &PhasePolicy::excursion_timeout_seconds)
        .def_readwrite("no_progress_corrections", &PhasePolicy::no_progress_corrections)
        .def_readwrite("progress_ms", &PhasePolicy::progress_ms)
        .def_readwrite("gain", &PhasePolicy::gain)
        .def_readwrite("max_mapping_span_ns", &PhasePolicy::max_mapping_span_ns)
        .def_readwrite("max_samples", &PhasePolicy::max_samples)
        .def("validate", &PhasePolicy::validate);
    py::class_<SyncConfig>(module, "SyncConfig")
        .def(py::init<>())
        .def_readwrite("group_id", &SyncConfig::group_id)
        .def_readwrite("reference_unit", &SyncConfig::reference_unit)
        .def_readwrite("fps", &SyncConfig::fps)
        .def_readwrite("network_mode", &SyncConfig::network_mode)
        .def_readwrite("clock_mode", &SyncConfig::clock_mode)
        .def_readwrite("phase_policy", &SyncConfig::phase_policy)
        .def_readwrite("clock_port", &SyncConfig::clock_port)
        .def_readwrite("nodes", &SyncConfig::nodes)
        .def_readwrite("units", &SyncConfig::units)
        .def_readwrite("clock_max_offset_ms", &SyncConfig::clock_max_offset_ms)
        .def_readwrite("clock_max_age_seconds", &SyncConfig::clock_max_age_seconds)
        .def_readwrite("request_timeout_seconds", &SyncConfig::request_timeout_seconds)
        .def_readwrite("startup_timeout_seconds", &SyncConfig::startup_timeout_seconds)
        .def_readwrite("join_timeout_seconds", &SyncConfig::join_timeout_seconds)
        .def("validate", &SyncConfig::validate)
        .def("node", &SyncConfig::node, py::arg("node_id"), py::return_value_policy::copy)
        .def("unit", &SyncConfig::unit, py::arg("unit_id"), py::return_value_policy::copy)
        .def("reference_node", &SyncConfig::reference_node)
        .def("to_json", &SyncConfig::to_json)
        .def("sync_json", &SyncConfig::sync_json)
        .def_static("from_file", [](const std::filesystem::path& path) {
            return SyncConfig::from_file(path.string());
        }, py::arg("path"));
    py::class_<ClockMapping>(module, "ClockMapping")
        .def_readonly("valid", &ClockMapping::valid)
        .def_readonly("offset_ns", &ClockMapping::offset_ns)
        .def_readonly("uncertainty_ns", &ClockMapping::uncertainty_ns)
        .def("map", &ClockMapping::map, py::arg("local_ns"));
    py::class_<SyncUnit, PythonSyncUnit, py::smart_holder>(module, "SyncUnit")
        .def(py::init<>())
        .def("bind", &SyncUnit::bind, py::arg("session_id"), py::arg("round_id"))
        .def("snapshot", &SyncUnit::snapshot)
        .def("adjust", &SyncUnit::adjust, py::arg("pulse"))
        .def("cancel", &SyncUnit::cancel)
        .def("fault", &SyncUnit::fault);
    py::class_<PhaseActuator::Submission>(module, "Submission")
        .def_readonly("duration_us", &PhaseActuator::Submission::duration_us)
        .def_readonly("correction_id", &PhaseActuator::Submission::correction_id)
        .def_readonly("restoration", &PhaseActuator::Submission::restoration)
        .def_readonly("round_id", &PhaseActuator::Submission::round_id);
    py::class_<PhaseActuator>(module, "PhaseActuator")
        .def(py::init<>())
        .def("bind", [](PhaseActuator& actuator, const std::string& session, std::uint64_t round) {
            return correction(actuator.bind(session, round));
        }, py::arg("session_id"), py::arg("round_id"))
        .def("snapshot", [](const PhaseActuator& actuator) { return correction(actuator.snapshot()); })
        .def("adjust", [](PhaseActuator& actuator, const std::string& session, std::uint64_t round,
                          const Pulse& pulse, std::int64_t now, std::int64_t mono,
                          std::int64_t baseline, std::int64_t minimum, std::int64_t maximum) {
            return correction(actuator.adjust(session, round, pulse.correction_id, pulse.delta_us,
                pulse.count, pulse.expires_ns, now, mono, baseline, minimum, maximum));
        }, py::arg("session_id"), py::arg("round_id"), py::arg("pulse"), py::arg("now_ns"),
           py::arg("monotonic_ns"), py::arg("baseline_us"), py::arg("minimum_us"), py::arg("maximum_us"))
        .def("cancel", [](PhaseActuator& actuator, const std::string& session, std::uint64_t round) {
            return correction(actuator.cancel(session, round));
        }, py::arg("session_id"), py::arg("round_id"))
        .def("next", &PhaseActuator::next, py::arg("baseline_us"), py::arg("monotonic_ns"))
        .def("completed", &PhaseActuator::completed, py::arg("submitted"), py::arg("sequence"), py::arg("timestamp_ns"));
    py::class_<SyncSession>(module, "SyncSession", py::release_gil_before_calling_cpp_dtor())
        .def(py::init<SyncConfig, std::string, LocalSyncUnits>(),
             py::arg("config"), py::arg("local_node"), py::arg("units"))
        .def("start", &SyncSession::start, py::call_guard<py::gil_scoped_release>())
        .def("set_node_addresses", &SyncSession::set_node_addresses, py::arg("addresses"),
             py::call_guard<py::gil_scoped_release>())
        .def("wait_ready", [](SyncSession& session, double seconds) {
            const auto duration = timeout(seconds);
            py::gil_scoped_release release;
            return session.wait_ready(duration);
        }, py::arg("timeout_seconds") = 60.0)
        .def("status", &SyncSession::status, py::call_guard<py::gil_scoped_release>())
        .def("clock", &SyncSession::clock, py::call_guard<py::gil_scoped_release>())
        .def("stop", &SyncSession::stop, py::call_guard<py::gil_scoped_release>())
        .def("close", &SyncSession::stop, py::call_guard<py::gil_scoped_release>())
        .def("__enter__", [](SyncSession& session) -> SyncSession& {
            py::gil_scoped_release release;
            session.start();
            return session;
        }, py::return_value_policy::reference_internal)
        .def("__exit__", [](SyncSession& session, const py::object&, const py::object&, const py::object&) {
            py::gil_scoped_release release;
            session.stop();
            return false;
        });
}
}
