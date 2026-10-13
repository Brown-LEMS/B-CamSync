#include "gain_warmup.hpp"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <vector>

using bcamsync::detail::GainWarmup;
namespace {
constexpr unsigned width = 320, height = 80;
constexpr std::int64_t epoch = 100000000000LL, half_second = 500000000;

void require(bool condition, const char *message) {
    if (!condition) throw std::runtime_error(message);
}
template<class Operation> void rejects(Operation operation) {
    bool rejected = false;
    try { operation(); } catch (const std::runtime_error &) { rejected = true; }
    require(rejected, "Expected invalid gain calibration to be rejected");
}
struct Scene {
    std::vector<std::uint8_t> pixels = std::vector<std::uint8_t>(width * height, 96);
    void uniform(std::uint8_t brightness) { std::fill(pixels.begin(), pixels.end(), brightness); }
    void view(unsigned index, std::uint8_t brightness) {
        for (unsigned y = 0; y < height; ++y)
            std::fill_n(pixels.begin() + y * width + index * (width / 4), width / 4, brightness);
    }
    bool observe(GainWarmup &controller, int sample, float actual_gain, int exposure = 9996) const {
        return controller.observe(pixels.data(), width, height, epoch + sample * half_second,
                                  actual_gain, exposure, 10000);
    }
};

void delayed_controls_and_quantization() {
    Scene scene;
    GainWarmup controller(1.0F, 8.0F);
    scene.uniform(64);
    require(!scene.observe(controller, 0, 1.0F), "Metering waits for its half-second interval");
    require(scene.observe(controller, 1, 1.0F), "Initial metering update");
    const auto desired = controller.requested;
    require(desired > 1.0F && !controller.locked, "Dark scene requests sensor gain without admitting frames");
    scene.uniform(96);
    scene.observe(controller, 2, 1.0F);
    scene.observe(controller, 3, 1.0F);
    require(!controller.locked && controller.state == "waiting_controls" && controller.requested == desired,
            "Delayed reported gain must withhold readiness and further gain changes");
    scene.observe(controller, 4, desired, 9800);
    require(!controller.locked && controller.state == "waiting_controls", "Delayed exposure must withhold readiness");
    scene.observe(controller, 5, desired, 9996);
    scene.observe(controller, 6, desired, 9996);
    require(!controller.locked, "Two settled measurements do not establish readiness");
    scene.observe(controller, 7, desired, 10101);
    require(!controller.locked && controller.state == "waiting_controls", "A control mismatch interrupts stable readiness observations");
    scene.observe(controller, 8, desired, 9996);
    scene.observe(controller, 9, desired, 9996);
    require(!controller.locked, "Readiness requires fresh consecutive settled observations");
    scene.observe(controller, 10, desired, 9996);
    require(controller.locked && controller.state == "target", "Sensor-quantized 9996us exposure is accepted for the 10000us request");
}

void convergence_and_freezing() {
    Scene scene;
    GainWarmup controller(1.0F, 8.0F);
    scene.uniform(16);
    scene.observe(controller, 0, 1.0F);
    for (int sample = 1; sample <= 39 && !controller.locked; ++sample) {
        // A synthetic sensor applies quantized requested analogue gain; pixels
        // reflect the scene's response rather than copying controller state.
        const auto reported = std::floor(controller.requested * 256.0F) / 256.0F;
        scene.uniform(static_cast<std::uint8_t>(std::min(255.0F, std::floor(16.0F * reported))));
        scene.observe(controller, sample, reported);
    }
    require(controller.locked && controller.state == "target", "Closed-loop gain must converge for a reachable scene brightness");
    require(controller.brightness >= 86.4 && controller.brightness <= 105.6, "Converged brightness lies in the target deadband");
    const auto gain = controller.requested;
    const auto brightness = controller.brightness;
    scene.uniform(0);
    require(!scene.observe(controller, 80, gain), "Locked startup gain does not restart metering after lighting changes");
    require(controller.locked && controller.requested == gain && controller.brightness == brightness,
            "Gain and calibration evidence remain frozen during acquisition");
}

void board_median_robustness() {
    Scene scene;
    scene.view(0, 0);
    scene.view(1, 96);
    scene.view(2, 96);
    scene.view(3, 255);
    GainWarmup controller(1.0F, 8.0F);
    scene.observe(controller, 0, 1.0F);
    for (int sample = 1; sample <= 3; ++sample) scene.observe(controller, sample, 1.0F);
    require(controller.locked && controller.requested == 1.0F && controller.brightness == 96.0,
            "One dark view and one bright view must not dominate board-level metering");
}

void gain_limit_states() {
    Scene scene;
    scene.uniform(0);
    GainWarmup dark(0.5F, 2.0F);
    scene.observe(dark, 0, 1.0F);
    for (int sample = 1; sample <= 39 && !dark.locked; ++sample)
        scene.observe(dark, sample, dark.requested);
    require(dark.locked && dark.state == "gain_limit_dark" && dark.requested == 2.0F,
            "Dark scenes finish explicitly at the sensor gain upper bound");

    GainWarmup policy_limit(0.5F, 16.0F);
    scene.observe(policy_limit, 0, 1.0F);
    for (int sample = 1; sample <= 39 && !policy_limit.locked; ++sample)
        scene.observe(policy_limit, sample, policy_limit.requested);
    require(policy_limit.locked && policy_limit.state == "gain_limit_dark" && policy_limit.requested == 8.0F,
            "Startup metering respects its 8x policy limit even when the sensor supports more");

    scene.uniform(255);
    GainWarmup bright(0.5F, 8.0F);
    scene.observe(bright, 0, 1.0F);
    for (int sample = 1; sample <= 3; ++sample) scene.observe(bright, sample, 1.0F);
    require(bright.locked && bright.state == "gain_limit_bright" && bright.requested == 1.0F,
            "Bright scenes finish explicitly at the selected 1x lower bound");
}

void invalid_observations_and_timeout() {
    Scene scene;
    GainWarmup controller(1.0F, 8.0F);
    scene.observe(controller, 0, 1.0F);
    scene.observe(controller, 1, 1.0F);
    scene.observe(controller, 2, 1.0F);
    scene.observe(controller, 3, std::numeric_limits<float>::quiet_NaN());
    require(!controller.locked && controller.state == "waiting_controls", "Nonfinite gain cannot establish startup readiness");
    scene.observe(controller, 4, 0.0F);
    require(!controller.locked && controller.state == "waiting_controls", "Nonpositive gain cannot establish startup readiness");
    scene.observe(controller, 5, 1.0F);
    scene.observe(controller, 6, 1.0F);
    require(!controller.locked, "Invalid observations reset the consecutive readiness window");
    rejects([&] {
        controller.observe(scene.pixels.data(), width, height, epoch + 20000000001LL,
                           1.0F, 9996, 10000);
    });
}

void invalid_sensor_ranges() {
    rejects([] { GainWarmup invalid(2.0F, 8.0F); });
    rejects([] { GainWarmup invalid(1.0F, 0.5F); });
    rejects([] { GainWarmup invalid(8.0F, 1.0F); });
    rejects([] { GainWarmup invalid(std::numeric_limits<float>::quiet_NaN(), 8.0F); });
    rejects([] { GainWarmup invalid(1.0F, std::numeric_limits<float>::infinity()); });
}
}

int main() {
    try {
        delayed_controls_and_quantization();
        convergence_and_freezing();
        board_median_robustness();
        gain_limit_states();
        invalid_observations_and_timeout();
        invalid_sensor_ranges();
        std::cout << "Gain warmup tests passed\n";
    } catch (const std::exception &error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
