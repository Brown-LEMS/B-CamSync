#include "bcamsync/session.hpp"
#include "python_frame.hpp"
#include "platform.hpp"
#include "sync_bindings.hpp"
#include <cmath>
#include <filesystem>
#include <limits>
#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <pybind11/stl/filesystem.h>
#include <stdexcept>

namespace py = pybind11;
using namespace bcamsync;
using bcamsync::python::FrameHandle;

namespace {
std::chrono::milliseconds timeout(double seconds) {
    if (!std::isfinite(seconds) || seconds < 0 ||
        seconds >= static_cast<double>(std::numeric_limits<std::int64_t>::max()) / 1000.0)
        throw std::invalid_argument("timeout_seconds must be a finite nonnegative duration");
    return std::chrono::milliseconds(static_cast<std::int64_t>(std::ceil(seconds * 1000.0)));
}

py::array frame_view(const FrameHandle& frame, unsigned index) {
    // view_data validates shape, index, and storage before a pointer is exposed.
    const auto* pixels = frame.packet->view_data(index);
    auto owner = std::make_unique<std::shared_ptr<const FramePacket>>(frame.packet);
    py::capsule capsule(owner.get(), [](void* pointer) {
        delete static_cast<std::shared_ptr<const FramePacket>*>(pointer);
    });
    owner.release();
    py::array result(py::dtype::of<std::uint8_t>(),
                     {static_cast<py::ssize_t>(frame.packet->height),
                      static_cast<py::ssize_t>(frame.packet->view_width())},
                     {static_cast<py::ssize_t>(frame.packet->width), py::ssize_t{1}}, pixels,
                     capsule);
    result.attr("setflags")(py::arg("write") = false);
    return result;
}
} // namespace

PYBIND11_MODULE(_native, module) {
    module.doc() = "B-CamSync native acquisition and synchronization";
    py::class_<detail::NetworkLink>(module, "_NetworkLink", py::release_gil_before_calling_cpp_dtor())
        .def(py::init<std::string, std::string>())
        .def("start", [](detail::NetworkLink& link, double seconds) {
            const auto duration = timeout(seconds);
            const auto now = monotonic_ns();
            if (duration.count() > (std::numeric_limits<std::int64_t>::max() - now) / 1000000LL)
                throw std::invalid_argument("network startup deadline overflows");
            std::atomic<bool> stopping{false};
            py::gil_scoped_release release;
            return link.start(stopping, now + duration.count() * 1000000LL);
        }, py::arg("timeout_seconds"))
        .def("stop", &detail::NetworkLink::stop, py::call_guard<py::gil_scoped_release>());
    module.def("enumerate_arducam_cameras", &enumerate_arducam_cameras,
               py::arg("profile") = "arducam_b0267_pi4", py::call_guard<py::gil_scoped_release>(),
               "List exact local libcamera camera IDs before creating a session.");
    py::class_<NodeConfig>(module, "NodeConfig")
        .def(py::init<>())
        .def_readwrite("id", &NodeConfig::id)
        .def_readwrite("address", &NodeConfig::address)
        .def_readwrite("interface", &NodeConfig::interface)
        .def_readwrite("port", &NodeConfig::port);
    py::class_<FakeOptions>(module, "FakeOptions")
        .def(py::init<>())
        .def_readwrite("width", &FakeOptions::width)
        .def_readwrite("height", &FakeOptions::height)
        .def_readwrite("initial_phase_us", &FakeOptions::initial_phase_us)
        .def_readwrite("drift_ppm", &FakeOptions::drift_ppm);
    py::class_<UnitConfig>(module, "UnitConfig")
        .def(py::init<>())
        .def_readwrite("id", &UnitConfig::id)
        .def_readwrite("node_id", &UnitConfig::node_id)
        .def_readwrite("camera_id", &UnitConfig::camera_id)
        .def_readwrite("profile", &UnitConfig::profile)
        .def_readwrite("exposure_us", &UnitConfig::exposure_us)
        .def_readwrite("analogue_gain", &UnitConfig::analogue_gain)
        .def_readwrite("auto_gain", &UnitConfig::auto_gain)
        .def_readwrite("queue_capacity", &UnitConfig::queue_capacity)
        .def_readwrite("pool_capacity", &UnitConfig::pool_capacity)
        .def_readwrite("fake", &UnitConfig::fake);
    py::class_<Config>(module, "Config")
        .def(py::init<>())
        .def(py::init<const Config&>(), py::arg("config"))
        .def_readwrite("group_id", &Config::group_id)
        .def_readwrite("reference_unit", &Config::reference_unit)
        .def_readwrite("backend", &Config::backend)
        .def_readwrite("fps", &Config::fps)
        .def_readwrite("network_mode", &Config::network_mode)
        .def_readwrite("clock_port", &Config::clock_port)
        .def_readwrite("nodes", &Config::nodes)
        .def_readwrite("units", &Config::units)
        .def_readwrite("clock_max_offset_ms", &Config::clock_max_offset_ms)
        .def_readwrite("clock_max_age_seconds", &Config::clock_max_age_seconds)
        .def_readwrite("request_timeout_seconds", &Config::request_timeout_seconds)
        .def_readwrite("startup_timeout_seconds", &Config::startup_timeout_seconds)
        .def("validate", &Config::validate)
        .def("node", &Config::node, py::arg("node_id"), py::return_value_policy::copy)
        .def("unit", &Config::unit, py::arg("unit_id"), py::return_value_policy::copy)
        .def("reference_node", &Config::reference_node)
        .def("to_json", &Config::to_json)
        .def("sync_json", &Config::sync_json)
        .def("sync_config", &Config::sync_config)
        .def_static(
            "from_file",
            [](const std::filesystem::path& path) { return Config::from_file(path.string()); },
            py::arg("path"));

    py::class_<UnitStatus>(module, "UnitStatus")
        .def_readonly("unit_id", &UnitStatus::unit_id)
        .def_readonly("state", &UnitStatus::state)
        .def_readonly("fault", &UnitStatus::fault)
        .def_readonly("ready", &UnitStatus::ready)
        .def_readonly("phase_ms", &UnitStatus::phase_ms)
        .def_readonly("p95_ms", &UnitStatus::p95_ms)
        .def_readonly("max_ms", &UnitStatus::max_ms)
        .def_readonly("measurement_end_ns", &UnitStatus::measurement_end_ns);
    py::class_<GroupStatus>(module, "GroupStatus")
        .def_readonly("state", &GroupStatus::state)
        .def_readonly("fault", &GroupStatus::fault)
        .def_readonly("ready", &GroupStatus::ready)
        .def_readonly("units", &GroupStatus::units);

    bcamsync::python::bind_sync(module);

    py::class_<FrameHandle>(module, "FramePacket",
                            "A board frame whose readonly views retain native storage.")
        .def_property_readonly("unit_id", [](const FrameHandle& f) { return f.packet->unit_id; })
        .def_property_readonly("sequence", [](const FrameHandle& f) { return f.packet->sequence; })
        .def_property_readonly("dropped_before",
                               [](const FrameHandle& f) { return f.packet->dropped_before; })
        .def_property_readonly("sensor_timestamp_ns",
                               [](const FrameHandle& f) { return f.packet->sensor_timestamp_ns; })
        .def_property_readonly("timestamp_ns",
                               [](const FrameHandle& f) { return f.packet->timestamp_ns; })
        .def_property_readonly("clock_valid", [](const FrameHandle& f) { return f.packet->clock_valid; })
        .def_property_readonly("requested_exposure_us", [](const FrameHandle& f) { return f.packet->requested_exposure_us; })
        .def_property_readonly("exposure_us", [](const FrameHandle& f) { return f.packet->exposure_us; })
        .def_property_readonly("requested_gain", [](const FrameHandle& f) { return f.packet->requested_gain; })
        .def_property_readonly("analogue_gain", [](const FrameHandle& f) { return f.packet->analogue_gain; })
        .def_property_readonly("gain_locked", [](const FrameHandle& f) { return f.packet->gain_locked; })
        .def_property_readonly("gain_state", [](const FrameHandle& f) { return f.packet->gain_state; })
        .def_property_readonly("width", [](const FrameHandle& f) { return f.packet->width; })
        .def_property_readonly("height", [](const FrameHandle& f) { return f.packet->height; })
        .def("view", &frame_view, py::arg("index"),
             "Return a readonly uint8 view; rows retain board stride.")
        .def_property_readonly("views", [](const FrameHandle& f) {
            py::tuple result(f.packet->views);
            for (unsigned index = 0; index < f.packet->views; ++index)
                result[index] = frame_view(f, index);
            return result;
        });

    py::class_<Session>(module, "Session", py::release_gil_before_calling_cpp_dtor())
        .def(py::init<Config, std::string>(), py::arg("config"), py::arg("local_node"))
        .def("start", &Session::start, py::call_guard<py::gil_scoped_release>())
        .def("set_node_addresses", &Session::set_node_addresses, py::arg("addresses"),
             py::call_guard<py::gil_scoped_release>(), "Refresh group IPv4 endpoints after stop without replacing camera ownership.")
        .def(
            "wait_ready",
            [](Session& session, double seconds) {
                const auto duration = timeout(seconds);
                py::gil_scoped_release release;
                return session.wait_ready(duration);
            },
            py::arg("timeout_seconds") = 60.0)
        .def(
            "read",
            [](Session& session, const std::string& unit_id, double seconds) -> py::object {
                const auto duration = timeout(seconds);
                std::shared_ptr<const FramePacket> packet;
                {
                    py::gil_scoped_release release;
                    packet = session.read(unit_id, duration);
                }
                if (!packet)
                    return py::none();
                return py::cast(FrameHandle{std::move(packet)});
            },
            py::arg("unit_id"), py::arg("timeout_seconds") = 1.0,
            "Wait for a board packet; return None when the timeout expires.")
        .def("status", &Session::status, py::call_guard<py::gil_scoped_release>())
        .def("stop", &Session::stop, py::call_guard<py::gil_scoped_release>())
        .def("close", &Session::stop, py::call_guard<py::gil_scoped_release>())
        .def(
            "__enter__",
            [](Session& session) -> Session& {
                py::gil_scoped_release release;
                session.start();
                return session;
            },
            py::return_value_policy::reference_internal)
        .def("__exit__",
             [](Session& session, const py::object&, const py::object&, const py::object&) {
                 py::gil_scoped_release release;
                 session.stop();
                 return false;
             });
}
