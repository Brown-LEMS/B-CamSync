#include "network_bindings.hpp"
#include "bcamsync/network.hpp"
#include <cmath>
#include <limits>
#include <stdexcept>

namespace py = pybind11;
namespace bcamsync::python {
namespace {
std::chrono::milliseconds timeout(double seconds) {
    if (!std::isfinite(seconds) || seconds <= 0 ||
        seconds >= static_cast<double>(std::numeric_limits<std::int64_t>::max()) / 1000.0)
        throw std::invalid_argument("timeout_seconds must be a finite positive duration");
    return std::chrono::milliseconds(static_cast<std::int64_t>(std::ceil(seconds * 1000.0)));
}
}
void bind_network(py::module_& module) {
    py::class_<NetworkSession>(module, "NetworkSession", py::release_gil_before_calling_cpp_dtor())
        .def(py::init<SyncConfig, std::string>(), py::arg("config"), py::arg("local_node"))
        .def("start", [](NetworkSession& session, double seconds) {
            const auto duration = timeout(seconds);
            py::gil_scoped_release release;
            session.start(duration);
        }, py::arg("timeout_seconds") = 60.0)
        .def("resolve", [](NetworkSession& session, double seconds) {
            const auto duration = timeout(seconds);
            py::gil_scoped_release release;
            return session.resolve(duration);
        }, py::arg("timeout_seconds") = 60.0)
        .def_property_readonly("address", [](const NetworkSession& session) {
            py::gil_scoped_release release;
            return session.address();
        })
        .def("request_stop", &NetworkSession::request_stop, py::call_guard<py::gil_scoped_release>())
        .def("stop", &NetworkSession::stop, py::call_guard<py::gil_scoped_release>())
        .def("close", &NetworkSession::stop, py::call_guard<py::gil_scoped_release>())
        .def("__enter__", [](NetworkSession& session) -> NetworkSession& {
            py::gil_scoped_release release;
            session.start();
            return session;
        }, py::return_value_policy::reference_internal)
        .def("__exit__", [](NetworkSession& session, const py::object&, const py::object&, const py::object&) {
            py::gil_scoped_release release;
            session.stop();
            return false;
        });
}
}
