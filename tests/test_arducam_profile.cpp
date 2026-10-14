#include "arducam_profile.hpp"
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

using bcamsync::detail::arducam_pipeline;
namespace {
void require(bool condition, const char *message) {
    if (!condition) throw std::runtime_error(message);
}

void profile_selection() {
    require(bcamsync::detail::arducam_width == 5120 && bcamsync::detail::arducam_height == 800 &&
                bcamsync::detail::arducam_views == 4, "B0267 profile owns packed four-view geometry");
    require(std::string(arducam_pipeline("arducam_b0267_pi4")) ==
                "/usr/share/libcamera/pipeline/rpi/vc4/rpi_apps.yaml",
            "Pi4 must select the VC4 pipeline");
    require(std::string(arducam_pipeline("arducam_b0267_pi5")).empty(),
            "Pi5 must select the default pipeline");
    require(bcamsync::detail::arducam_raw_packing("arducam_b0267_pi4") ==
                bcamsync::detail::ArducamRawPacking::r8,
            "Pi4 must acquire R8 buffers");
    require(bcamsync::detail::arducam_raw_packing("arducam_b0267_pi5") ==
                bcamsync::detail::ArducamRawPacking::r16,
            "Pi5 must acquire PiSP R16 buffers");
    bool rejected = false;
    try { arducam_pipeline("unknown"); }
    catch (const std::invalid_argument &) { rejected = true; }
    require(rejected, "Unknown profiles must be rejected");
    rejected = false;
    try { bcamsync::detail::arducam_raw_packing("unknown"); }
    catch (const std::invalid_argument &) { rejected = true; }
    require(rejected, "Unknown raw profiles must be rejected");
}

void pixel_conversion(bcamsync::detail::ArducamRawPacking packing, unsigned padding) {
    using namespace bcamsync::detail;
    const unsigned bytes = arducam_raw_pixel_bytes(packing);
    const unsigned stride = arducam_width * bytes + padding;
    std::vector<std::uint8_t> source(static_cast<std::size_t>(stride) * arducam_height, 0xfe);
    std::vector<std::uint8_t> target(static_cast<std::size_t>(arducam_width) * arducam_height);
    std::vector<std::uint8_t> expected(target.size());
    for (unsigned y = 0; y < arducam_height; ++y) {
        for (unsigned x = 0; x < arducam_width; ++x) {
            const auto signal = static_cast<std::uint8_t>((x + 7 * y) % 256);
            const auto offset = static_cast<std::size_t>(y) * stride + x * bytes;
            source[offset + bytes - 1] = signal;
            if (bytes == 2) source[offset] = static_cast<std::uint8_t>(255 - signal);
            expected[static_cast<std::size_t>(y) * arducam_width + x] = signal;
        }
    }
    copy_arducam_pixels(source.data(), target.data(), stride, packing);
    require(target == expected, "Raw packing and row padding must preserve all four gray8 views");
    bool rejected = false;
    try { copy_arducam_pixels(source.data(), target.data(), arducam_width * bytes - 1, packing); }
    catch (const std::invalid_argument &) { rejected = true; }
    require(rejected, "Copying must reject a stride shorter than one packed row");
}
} // namespace

int main() {
    try {
        profile_selection();
        for (auto packing : {bcamsync::detail::ArducamRawPacking::r8,
                             bcamsync::detail::ArducamRawPacking::r16}) {
            pixel_conversion(packing, 0);
            pixel_conversion(packing, 64);
        }
        std::cout << "Arducam profile tests passed\n";
    } catch (const std::exception &error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
