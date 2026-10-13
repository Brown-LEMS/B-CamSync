#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>

namespace bcamsync::detail {
/// Startup sensor-gain feedback with fixed exposure; image pixels are never modified.
class GainWarmup {
    float minimum_, maximum_;
    int stable_ = 0;
    std::int64_t started_ = -1, next_sample_ = 0;
public:
    float requested = 1.0F;
    bool locked = false;
    std::string state = "settling";
    double brightness = 0;

    GainWarmup(float sensor_minimum, float sensor_maximum)
        : minimum_(std::max(1.0F, sensor_minimum)), maximum_(std::min(8.0F, sensor_maximum)) {
        if (!std::isfinite(sensor_minimum) || !std::isfinite(sensor_maximum) ||
            minimum_ > maximum_ || requested < minimum_ || requested > maximum_)
            throw std::runtime_error("Camera does not support startup gain calibration from 1x");
    }

    /// Return true on each half-second metering update, after delayed controls are observed.
    bool observe(const std::uint8_t *pixels, unsigned width, unsigned height,
                 std::int64_t sensor_timestamp_ns, float reported_gain,
                 int exposure_us, int requested_exposure_us) {
        if (locked) return false;
        if (started_ < 0) {
            started_ = sensor_timestamp_ns;
            next_sample_ = sensor_timestamp_ns + 500000000;
        }
        if (sensor_timestamp_ns - started_ > 20000000000LL)
            throw std::runtime_error("Startup analogue gain did not settle within 20 seconds");
        if (sensor_timestamp_ns < next_sample_) return false;
        next_sample_ = sensor_timestamp_ns + 500000000;
        if (!std::isfinite(reported_gain) || reported_gain <= 0 ||
            std::abs(reported_gain - requested) > 0.05F * requested ||
            std::abs(exposure_us - requested_exposure_us) > 100) {
            stable_ = 0;
            state = "waiting_controls";
            return true;
        }
        std::array<double, 4> means{};
        const unsigned view_width = width / 4;
        for (unsigned view = 0; view < 4; ++view) {
            std::array<unsigned, 256> histogram{};
            unsigned count = 0;
            for (unsigned y = height / 10; y < height * 9 / 10; y += 8)
                for (unsigned x = view_width / 10; x < view_width * 9 / 10; x += 8) {
                    ++histogram[pixels[static_cast<std::size_t>(y) * width + view * view_width + x]];
                    ++count;
                }
            unsigned cumulative = 0, kept = 0;
            double sum = 0;
            for (unsigned value = 0; value < 256; ++value) {
                const unsigned lo = std::max(cumulative, count / 10);
                cumulative += histogram[value];
                const unsigned hi = std::min(cumulative, count * 9 / 10);
                if (hi > lo) {
                    sum += static_cast<double>(value) * (hi - lo);
                    kept += hi - lo;
                }
            }
            means[view] = sum / kept;
        }
        std::sort(means.begin(), means.end());
        brightness = (means[1] + means[2]) / 2;
        const bool at_low = requested <= minimum_ * 1.001F;
        const bool at_high = requested >= maximum_ * 0.999F;
        if (brightness >= 86.4 && brightness <= 105.6) state = "target";
        else if (brightness < 86.4 && at_high) state = "gain_limit_dark";
        else if (brightness > 105.6 && at_low) state = "gain_limit_bright";
        else {
            const float factor = static_cast<float>(std::clamp(96.0 / std::max(1.0, brightness), 0.67, 1.5));
            requested = std::clamp(requested * factor, minimum_, maximum_);
            stable_ = 0;
            state = "adjusting";
            return true;
        }
        locked = ++stable_ >= 3;
        return true;
    }
};
}
