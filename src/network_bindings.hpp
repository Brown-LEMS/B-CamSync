#pragma once
#include <pybind11/pybind11.h>

namespace bcamsync::python {
void bind_network(pybind11::module_& module);
}
