#pragma once
#include <pybind11/pybind11.h>

namespace bcamsync::python {
void bind_sync(pybind11::module_& module);
}
