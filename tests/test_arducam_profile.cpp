#include "arducam_profile.hpp"
#include <iostream>
#include <stdexcept>
#include <string>

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
    bool rejected = false;
    try { arducam_pipeline("unknown"); }
    catch (const std::invalid_argument &) { rejected = true; }
    require(rejected, "Unknown profiles must be rejected");
}
} // namespace

int main() {
    try {
        profile_selection();
        std::cout << "Arducam profile tests passed\n";
    } catch (const std::exception &error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
