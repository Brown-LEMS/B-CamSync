#pragma once
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>

namespace bcamsync::detail {
inline constexpr unsigned arducam_width = 5120, arducam_height = 800, arducam_views = 4;
enum class ArducamRawPacking { r8, r16 };
inline const char *arducam_pipeline(const std::string &name) {
    if (name == "arducam_b0267_pi4")
        return "/usr/share/libcamera/pipeline/rpi/vc4/rpi_apps.yaml";
    if (name == "arducam_b0267_pi5") return "";
    throw std::invalid_argument("Unknown Arducam profile: " + name);
}

inline ArducamRawPacking arducam_raw_packing(const std::string &name) {
    if (name == "arducam_b0267_pi4") return ArducamRawPacking::r8;
    if (name == "arducam_b0267_pi5") return ArducamRawPacking::r16;
    throw std::invalid_argument("Unknown Arducam profile: " + name);
}

inline unsigned arducam_raw_pixel_bytes(ArducamRawPacking packing) {
    return packing == ArducamRawPacking::r16 ? 2u : 1u;
}

/// PiSP R16 stores the 8-bit sensor signal in the high byte of each LE word.
inline void copy_arducam_pixels(const std::uint8_t *source, std::uint8_t *target,
                                unsigned stride, ArducamRawPacking packing) {
    if (stride < arducam_width * arducam_raw_pixel_bytes(packing))
        throw std::invalid_argument("Raw camera stride is too short");
    if (packing == ArducamRawPacking::r8 && stride == arducam_width) {
        std::memcpy(target, source, static_cast<std::size_t>(arducam_width) * arducam_height);
        return;
    }
    for (unsigned y = 0; y < arducam_height; ++y) {
        const auto *row = source + static_cast<std::size_t>(y) * stride;
        auto *out = target + static_cast<std::size_t>(y) * arducam_width;
        if (packing == ArducamRawPacking::r8) std::memcpy(out, row, arducam_width);
        else for (unsigned x = 0; x < arducam_width; ++x) out[x] = row[2 * x + 1];
    }
}

} // namespace bcamsync::detail
