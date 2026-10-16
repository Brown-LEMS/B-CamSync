#include "bcamsync/fake.hpp"
#include "bcamsync/actuator.hpp"
#include "bcamsync/frame_queue.hpp"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <limits>
#include <stdexcept>
#include <thread>

namespace bcamsync {
namespace {
constexpr std::int64_t second_ns = 1000000000;
std::int64_t sensor_clock_ns() {
    return boot_time_ns();
}
CorrectionStatus correction_status(const PhaseActuator::State& state) {
    return {state.state, state.correction_id, state.restored_timestamp_ns};
}
class FakeBackend final : public AcquisitionBackend {
    UnitConfig config_;
    FakeOptions options_;
    unsigned fps_;
    mutable std::mutex lifecycle_mutex_, state_mutex_;
    std::condition_variable changed_;
    std::atomic<bool> stopping_{true};
    std::thread worker_;
    FrameQueue images_;
    std::unique_ptr<PhaseActuator> actuator_;
    std::deque<FrameTiming> timing_;
    std::string session_id_, fault_;
    std::uint64_t round_id_ = 0;
    std::int64_t baseline_us() const { return 1000000 / fps_; }
    void capture(std::int64_t sensor_ns, std::int64_t deadline_ns) noexcept {
        try {
            std::uint64_t sequence = 0;
            double fractional_ns = 0;
            auto submitted = actuator_->next(baseline_us(), monotonic_ns());
            while (!stopping_) {
                {
                    std::unique_lock<std::mutex> lock(state_mutex_);
                    while (!stopping_) {
                        const auto remaining = deadline_ns - monotonic_ns();
                        if (remaining <= 0)
                            break;
                        changed_.wait_for(lock, std::chrono::nanoseconds(remaining),
                                          [&] { return stopping_.load(); });
                    }
                }
                if (stopping_)
                    break;
                FrameTiming timing;
                timing.camera_sequence = sequence;
                timing.sensor_timestamp_ns = sensor_ns;
                timing.timestamp_ns = sensor_ns;
                timing.requested_duration_us = timing.reported_duration_us = submitted.duration_us;
                timing.correction_id = submitted.correction_id;
                actuator_->completed(submitted, sequence, timing.timestamp_ns);
                {
                    std::lock_guard<std::mutex> lock(state_mutex_);
                    timing_.push_back(timing);
                    while (!timing_.empty() &&
                           (timing_.size() > 512 ||
                            timing_.front().timestamp_ns < timing.timestamp_ns - 6 * second_ns))
                        timing_.pop_front();
                    changed_.notify_all();
                }
                auto storage = images_.acquire_storage();
                if (storage) {
                    for (std::size_t i = 0; i < storage->pixels.size(); ++i)
                        storage->pixels[i] = static_cast<std::uint8_t>(sequence + i);
                    auto packet = std::make_shared<FramePacket>();
                    packet->unit_id = config_.id;
                    packet->sequence = sequence;
                    packet->sensor_timestamp_ns = timing.sensor_timestamp_ns;
                    packet->timestamp_ns = timing.timestamp_ns;
                    packet->requested_exposure_us = packet->exposure_us = config_.exposure_us;
                    packet->requested_gain = packet->analogue_gain = config_.analogue_gain;
                    packet->gain_locked = true;
                    packet->gain_state = "fake";
                    packet->width = options_.width;
                    packet->height = options_.height;
                    packet->views = 4;
                    packet->storage = std::move(storage);
                    images_.publish(std::move(packet));
                }
                ++sequence;
                submitted = actuator_->next(baseline_us(), monotonic_ns());
                const double exact_interval = static_cast<double>(submitted.duration_us) * 1000 *
                                                  (1 + options_.drift_ppm * 1e-6) +
                                              fractional_ns;
                const auto interval = static_cast<std::int64_t>(std::llround(exact_interval));
                fractional_ns = exact_interval - static_cast<double>(interval);
                sensor_ns += interval;
                deadline_ns += interval;
            }
        } catch (const std::exception& error) {
            {
                std::lock_guard<std::mutex> lock(state_mutex_);
                fault_ = error.what();
                stopping_ = true;
                changed_.notify_all();
            }
            images_.fail(error.what());
        } catch (...) {
            {
                std::lock_guard<std::mutex> lock(state_mutex_);
                fault_ = "Unknown fake capture failure";
                stopping_ = true;
                changed_.notify_all();
            }
            images_.fail("Unknown fake capture failure");
        }
    }

  public:
    FakeBackend(UnitConfig config, FakeOptions options, unsigned fps)
        : config_(std::move(config)), options_(options), fps_(fps) {
        const auto pixels = static_cast<std::uint64_t>(options_.width) * options_.height;
        if (config_.id.empty() || !options_.width || !options_.height || options_.width % 4 ||
            pixels > std::vector<std::uint8_t>{}.max_size() || !fps_ || fps_ > 1000000 ||
            !config_.queue_capacity ||
            static_cast<std::uint64_t>(config_.pool_capacity) <= config_.queue_capacity ||
            pixels > std::numeric_limits<std::size_t>::max() / config_.pool_capacity)
            throw std::invalid_argument("Invalid fake capture settings");
        if (!std::isfinite(options_.drift_ppm) || std::abs(options_.drift_ppm) > 1000 ||
            options_.initial_phase_us < -1000000 || options_.initial_phase_us > 1000000)
            throw std::invalid_argument("Invalid fake sensor phase or drift");
    }
    ~FakeBackend() override { stop(); }
    void start(const std::string& session, std::uint64_t round) override {
        std::lock_guard<std::mutex> lifecycle(lifecycle_mutex_);
        if (worker_.joinable())
            throw std::runtime_error("Fake camera is already started; stop before restarting");
        auto actuator = std::make_unique<PhaseActuator>();
        actuator->bind(session, round);
        images_.open(static_cast<std::size_t>(options_.width) * options_.height,
                     config_.queue_capacity, config_.pool_capacity);
        try {
            actuator_ = std::move(actuator);
            session_id_ = session;
            round_id_ = round;
            {
                std::lock_guard<std::mutex> lock(state_mutex_);
                timing_.clear();
                fault_.clear();
            }
            const auto sensor_now = sensor_clock_ns();
            const auto mono = monotonic_ns();
            const auto period = baseline_us() * 1000;
            auto first = sensor_now - sensor_now % period + options_.initial_phase_us * 1000 % period;
            while (first <= sensor_now)
                first += period;
            stopping_ = false;
            worker_ = std::thread([this, first, mono, sensor_now] {
                capture(first, mono + (first - sensor_now));
            });
        } catch (...) {
            stopping_ = true;
            images_.close();
            actuator_.reset();
            throw;
        }
    }
    void stop() override {
        std::lock_guard<std::mutex> lifecycle(lifecycle_mutex_);
        if (actuator_)
            actuator_->cancel_active();
        if (worker_.joinable() && !stopping_ && actuator_) {
            const auto active = actuator_->snapshot().state;
            if (active == "submitting" || active == "settling") {
                const auto budget = std::chrono::milliseconds(
                    std::max<std::int64_t>(2000, 2 * (baseline_us() + 1000) / 1000 + 1000));
                std::unique_lock<std::mutex> lock(state_mutex_);
                if (!changed_.wait_for(lock, budget,
                                       [&] {
                                           return !fault_.empty() || stopping_.load() ||
                                                  actuator_->snapshot().state == "restored";
                                       }) &&
                    fault_.empty())
                    fault_ = "Baseline fake frame period restoration timed out during stop";
            }
        }
        {
            std::lock_guard<std::mutex> lock(state_mutex_);
            stopping_ = true;
            changed_.notify_all();
        }
        if (worker_.joinable())
            worker_.join();
        images_.close();
    }
    TimingSnapshot snapshot() const override {
        std::lock_guard<std::mutex> lifecycle(lifecycle_mutex_);
        TimingSnapshot result;
        result.unit_id = config_.id;
        {
            std::lock_guard<std::mutex> lock(state_mutex_);
            result.frames.assign(timing_.begin(), timing_.end());
            result.snapshot_timestamp_ns = sensor_clock_ns();
        }
        if (actuator_)
            result.correction = correction_status(actuator_->snapshot());
        return result;
    }
    CorrectionStatus adjust(const Pulse& pulse) override {
        std::lock_guard<std::mutex> lifecycle(lifecycle_mutex_);
        if (stopping_ || !worker_.joinable())
            throw std::runtime_error("Cannot adjust a stopped fake camera");
        return correction_status(actuator_->adjust(session_id_, round_id_, pulse.correction_id,
                                                   pulse.delta_us, pulse.count, pulse.expires_ns,
                                                   sensor_clock_ns(), monotonic_ns(), baseline_us(),
                                                   std::max<std::int64_t>(1, baseline_us() - 1000), baseline_us() + 1000));
    }
    void cancel() override {
        std::lock_guard<std::mutex> lifecycle(lifecycle_mutex_);
        if (actuator_)
            actuator_->cancel(session_id_, round_id_);
    }
    std::shared_ptr<const FramePacket> read(std::chrono::milliseconds timeout) override {
        return images_.read(timeout);
    }
    std::string fault() const override {
        std::lock_guard<std::mutex> lock(state_mutex_);
        return fault_;
    }
};
} // namespace
std::vector<std::unique_ptr<AcquisitionBackend>>
make_fake_backends(const std::vector<UnitConfig>& units, const std::vector<FakeOptions>& options, unsigned fps) {
    if (units.size() != options.size())
        throw std::invalid_argument("Fake options must match unit count");
    std::vector<std::unique_ptr<AcquisitionBackend>> result;
    result.reserve(units.size());
    for (std::size_t i = 0; i < units.size(); ++i)
        result.push_back(std::make_unique<FakeBackend>(units[i], options[i], fps));
    return result;
}
std::vector<std::unique_ptr<AcquisitionBackend>>
make_fake_backends(const std::vector<UnitConfig>& units, unsigned fps) {
    std::vector<FakeOptions> options;
    options.reserve(units.size());
    for (const auto& unit : units)
        options.push_back(unit.fake);
    return make_fake_backends(units, options, fps);
}
} // namespace bcamsync
