#pragma once
#include <stdexcept>
#include <string>

namespace bcamsync::detail {
inline constexpr unsigned arducam_width = 5120, arducam_height = 800, arducam_views = 4;
inline const char *arducam_pipeline(const std::string &name) {
    if (name == "arducam_b0267_pi4")
        return "/usr/share/libcamera/pipeline/rpi/vc4/rpi_apps.yaml";
    if (name == "arducam_b0267_pi5") return "";
    throw std::invalid_argument("Unknown Arducam profile: " + name);
}

} // namespace bcamsync::detail
