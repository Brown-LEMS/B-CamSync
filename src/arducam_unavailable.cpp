#include "bcamsync/backend.hpp"
#include <stdexcept>

namespace bcamsync {
std::vector<std::unique_ptr<AcquisitionBackend>> make_arducam_backends(const std::vector<UnitConfig> &) {
    throw std::runtime_error("Arducam acquisition requires a Linux build with BCAMSYNC_WITH_LIBCAMERA=ON");
}
}
