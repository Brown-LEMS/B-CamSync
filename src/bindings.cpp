#include "bcamsync/sync.hpp"
#include "sync_bindings.hpp"
#include "network_bindings.hpp"
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;
using namespace bcamsync;

PYBIND11_MODULE(_native, module) {
    module.doc() = "B-CamSync synchronization core";
    py::class_<NodeConfig>(module, "NodeConfig")
        .def(py::init<>())
        .def_readwrite("id", &NodeConfig::id)
        .def_readwrite("address", &NodeConfig::address)
        .def_readwrite("interface", &NodeConfig::interface)
        .def_readwrite("port", &NodeConfig::port);
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
    bcamsync::python::bind_network(module);

}
