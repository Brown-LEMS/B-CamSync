#include "bcamsync/actuator.hpp"
#include "bcamsync/backend.hpp"
#include "bcamsync/frame_queue.hpp"
#include "gain_warmup.hpp"

#include <libcamera/camera.h>
#include <libcamera/camera_manager.h>
#include <libcamera/control_ids.h>
#include <libcamera/controls.h>
#include <libcamera/formats.h>
#include <libcamera/framebuffer.h>
#include <libcamera/framebuffer_allocator.h>
#include <libcamera/request.h>

#include <linux/dma-buf.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <map>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <sstream>
#include <thread>
#include <utility>

namespace bcamsync {
namespace {
constexpr std::int64_t second_ns = 1000000000LL;
constexpr const char *arducam_profile = "arducam_b0267_pi4";
constexpr const char *pi4_pipeline = "/usr/share/libcamera/pipeline/rpi/vc4/rpi_apps.yaml";
std::mutex progress_mutex;

const char *profile_pipeline(const std::string &profile) {
    if (profile != arducam_profile) throw std::invalid_argument("Unknown Arducam profile: " + profile);
    return pi4_pipeline;
}

std::int64_t boot_ns() {
    timespec value{};
    if (clock_gettime(CLOCK_BOOTTIME, &value)) throw std::runtime_error("CLOCK_BOOTTIME unavailable");
    return value.tv_sec * second_ns + value.tv_nsec;
}

CorrectionStatus correction_status(const PhaseActuator::State &state) {
    return {state.state, state.correction_id, state.restored_timestamp_ns};
}

std::atomic<bool> manager_active{false};
struct ManagerLease {
    ManagerLease() {
        if (manager_active.exchange(true)) throw std::runtime_error("Only one Arducam acquisition session may exist per process");
    }
    ~ManagerLease() { manager_active.store(false); }
    ManagerLease(const ManagerLease &) = delete;
    ManagerLease &operator=(const ManagerLease &) = delete;
};

struct PipelineEnvironment {
    std::string previous;
    bool had_previous = false, changed = false;
    explicit PipelineEnvironment(const std::string &pipeline) {
        if (pipeline.empty()) return;
        if (access(pipeline.c_str(), R_OK)) throw std::runtime_error("Pipeline configuration is not readable: " + pipeline);
        if (const auto *value = std::getenv("LIBCAMERA_RPI_CONFIG_FILE")) {
            previous = value;
            had_previous = true;
        }
        if (setenv("LIBCAMERA_RPI_CONFIG_FILE", pipeline.c_str(), 1)) throw std::runtime_error("Cannot configure libcamera pipeline environment");
        changed = true;
    }
    ~PipelineEnvironment() {
        if (!changed) return;
        if (had_previous) setenv("LIBCAMERA_RPI_CONFIG_FILE", previous.c_str(), 1);
        else unsetenv("LIBCAMERA_RPI_CONFIG_FILE");
    }
    PipelineEnvironment(const PipelineEnvironment &) = delete;
    PipelineEnvironment &operator=(const PipelineEnvironment &) = delete;
};

struct Manager {
    // The lease is constructed before the SDK manager and released after its destruction.
    ManagerLease lease;
    PipelineEnvironment pipeline_environment;
    libcamera::CameraManager camera_manager;
    explicit Manager(const std::string &pipeline) : pipeline_environment(pipeline) {
        if (camera_manager.start()) throw std::runtime_error("libcamera CameraManager failed to start");
    }
};

struct Mapping {
    void *base = MAP_FAILED;
    std::size_t length = 0;
    const std::uint8_t *pixels = nullptr;
    Mapping(const libcamera::FrameBuffer::Plane &plane, long page) {
        const std::size_t aligned = plane.offset / static_cast<std::size_t>(page) * static_cast<std::size_t>(page);
        const std::size_t extra = plane.offset - aligned;
        length = plane.length + extra;
        base = mmap(nullptr, length, PROT_READ, MAP_SHARED, plane.fd.get(), static_cast<off_t>(aligned));
        if (base == MAP_FAILED) throw std::runtime_error("Cannot map camera raw buffer");
        pixels = static_cast<const std::uint8_t *>(base) + extra;
    }
    ~Mapping() { if (base != MAP_FAILED) munmap(base, length); }
    Mapping(const Mapping &) = delete;
    Mapping &operator=(const Mapping &) = delete;
};

class DmaRead {
    int fd_;
    bool active_ = true;
public:
    explicit DmaRead(int fd) : fd_(fd) {
        dma_buf_sync sync{DMA_BUF_SYNC_START | DMA_BUF_SYNC_READ};
        if (ioctl(fd_, DMA_BUF_IOCTL_SYNC, &sync)) throw std::runtime_error("Camera DMA read synchronization failed");
    }
    void finish() {
        dma_buf_sync sync{DMA_BUF_SYNC_END | DMA_BUF_SYNC_READ};
        if (ioctl(fd_, DMA_BUF_IOCTL_SYNC, &sync)) throw std::runtime_error("Camera DMA read completion failed");
        active_ = false;
    }
    ~DmaRead() {
        if (active_) {
            dma_buf_sync sync{DMA_BUF_SYNC_END | DMA_BUF_SYNC_READ};
            ioctl(fd_, DMA_BUF_IOCTL_SYNC, &sync);
        }
    }
};

class ArducamBackend final : public AcquisitionBackend {
    UnitConfig config_;
    std::shared_ptr<Manager> manager_;
    std::shared_ptr<libcamera::Camera> camera_;
    std::unique_ptr<libcamera::CameraConfiguration> configuration_;
    std::unique_ptr<libcamera::FrameBufferAllocator> allocator_;
    std::vector<std::unique_ptr<libcamera::Request>> requests_;
    std::map<libcamera::FrameBuffer *, std::unique_ptr<Mapping>> mappings_;
    struct Submission {
        PhaseActuator::Submission phase;
        int exposure_us = 0;
        float gain = 1.0F;
    };
    struct Completion {
        libcamera::Request *request = nullptr;
        libcamera::FrameBuffer *buffer = nullptr;
        Submission submission;
        FrameTiming timing;
        int exposure_us = 0;
        float gain = 0;
        bool startup = false;
    };
    std::map<libcamera::Request *, Submission> submitted_;
    libcamera::Stream *raw_ = nullptr;
    unsigned stride_ = 0;
    std::int64_t minimum_duration_ = 0, maximum_duration_ = 0;
    bool acquired_ = false, camera_started_ = false, connected_ = false, common_time_ = false;
    std::atomic<bool> running_{false};
    mutable std::mutex lifecycle_mutex_;
    std::unique_ptr<PhaseActuator> actuator_;
    std::unique_ptr<detail::GainWarmup> gain_warmup_;
    FrameStorage metering_storage_;
    float requested_gain_ = 1.0F;
    bool gain_locked_ = false;
    std::string gain_state_ = "waiting_controls";
    std::int64_t next_manual_progress_ = 0;
    std::string session_id_;
    std::uint64_t round_id_ = 0, sequence_ = 0;
    std::int64_t last_sensor_ = 0, last_timestamp_ = 0;

    mutable std::mutex state_mutex_;
    std::condition_variable timing_changed_;
    std::deque<FrameTiming> timing_;
    FrameQueue images_;
    std::string fault_;
    bool camera_ready_ = false;

    std::mutex delivery_mutex_;
    std::condition_variable delivery_changed_;
    std::deque<Completion> deliveries_;
    bool delivery_stop_ = true;
    std::thread copier_;

    std::int64_t domain_now() const { return common_time_ ? realtime_ns() : boot_ns(); }

    void fail(const char *message) noexcept {
        try {
            {
                std::lock_guard<std::mutex> lock(state_mutex_);
                if (fault_.empty()) fault_ = message;
                timing_changed_.notify_all();
            }
            images_.fail(message);
        } catch (...) {
            running_.store(false);
            timing_changed_.notify_all();
            images_.close();
        }
    }

    void complete(libcamera::Request *request) noexcept {
        try {
            if (!running_.load()) return;
            if (request->status() == libcamera::Request::RequestCancelled) {
                fail("Camera cancelled a running request");
                return;
            }
            if (request->status() != libcamera::Request::RequestComplete)
                throw std::runtime_error("Incomplete camera request");
            Completion completion;
            completion.request = request;
            completion.buffer = request->buffers().at(raw_);
            const auto &metadata = completion.buffer->metadata();
            completion.startup = metadata.status == libcamera::FrameMetadata::FrameStartup;
            if (!completion.startup) {
                if (metadata.status != libcamera::FrameMetadata::FrameSuccess)
                    throw std::runtime_error("Unsuccessful camera raw buffer");
                const auto sensor = request->metadata().get(libcamera::controls::SensorTimestamp);
                const auto duration = request->metadata().get(libcamera::controls::FrameDuration);
                const auto exposure = request->metadata().get(libcamera::controls::ExposureTime);
                const auto gain = request->metadata().get(libcamera::controls::AnalogueGain);
                if (!sensor || !duration || !exposure || !gain)
                    throw std::runtime_error("Camera requires sensor timestamp, duration, exposure, and analogue-gain metadata");
                if (*duration <= 0 || *exposure <= 0 || !std::isfinite(*gain) || *gain <= 0)
                    throw std::runtime_error("Camera reported invalid frame controls");
                completion.submission = submitted_.at(request);
                auto &timing = completion.timing;
                timing.camera_sequence = metadata.sequence;
                timing.sensor_timestamp_ns = *sensor;
                timing.timestamp_ns = *sensor;
                if (common_time_) {
                    const auto before = boot_ns(), real = realtime_ns(), after = boot_ns();
                    timing.mapping_span_ns = after - before;
                    timing.timestamp_ns += real - (before + (after - before) / 2);
                }
                timing.requested_duration_us = completion.submission.phase.duration_us;
                timing.reported_duration_us = *duration;
                timing.correction_id = completion.submission.phase.correction_id;
                completion.exposure_us = *exposure;
                completion.gain = *gain;
            }
            std::lock_guard<std::mutex> lock(delivery_mutex_);
            if (delivery_stop_) return;
            deliveries_.push_back(completion);
            delivery_changed_.notify_one();
        } catch (const std::exception &error) { fail(error.what()); }
        catch (...) { fail("Unknown camera completion callback failure"); }
    }

    void requeue(libcamera::Request *request) {
        if (!running_.load()) return;
        request->reuse(libcamera::Request::ReuseBuffers);
        const auto control = actuator_->next(1000000 / config_.fps, monotonic_ns());
        submitted_.at(request) = {control, config_.exposure_us, requested_gain_};
        const std::int64_t limits[]{control.duration_us, control.duration_us};
        request->controls().set(libcamera::controls::FrameDurationLimits, libcamera::Span<const std::int64_t, 2>(limits));
        request->controls().set(libcamera::controls::ExposureTime, config_.exposure_us);
        request->controls().set(libcamera::controls::AnalogueGain, requested_gain_);
        if (camera_->queueRequest(request) < 0) throw std::runtime_error("Camera request submission failed");
    }

    void copy_pixels(libcamera::FrameBuffer *buffer, FrameStorage &storage) {
        DmaRead access(buffer->planes().front().fd.get());
        const auto *source = mappings_.at(buffer)->pixels;
        if (stride_ == config_.width) std::memcpy(storage.pixels.data(), source, storage.pixels.size());
        else for (unsigned y = 0; y < config_.height; ++y)
            std::memcpy(storage.pixels.data() + static_cast<std::size_t>(y) * config_.width,
                        source + static_cast<std::size_t>(y) * stride_, config_.width);
        access.finish();
    }

    void gain_progress(const Completion &completion) const {
        std::ostringstream message;
        message << "B-CamSync " << config_.id << " gain warmup: state=" << gain_state_
                << ", brightness=" << (gain_warmup_ ? gain_warmup_->brightness : 0)
                << ", requested_gain=" << requested_gain_ << ", reported_gain=" << completion.gain
                << ", exposure_us=" << completion.exposure_us << ", locked=" << gain_locked_ << '\n';
        std::lock_guard<std::mutex> lock(progress_mutex);
        std::clog << message.str() << std::flush;
    }

    void warmup(const Completion &completion) {
        bool report = false;
        if (gain_warmup_) {
            copy_pixels(completion.buffer, metering_storage_);
            report = gain_warmup_->observe(metering_storage_.pixels.data(), config_.width, config_.height,
                                          completion.timing.sensor_timestamp_ns, completion.gain,
                                          completion.exposure_us, config_.exposure_us);
            requested_gain_ = gain_warmup_->requested;
            gain_locked_ = gain_warmup_->locked;
            gain_state_ = gain_warmup_->state;
        } else {
            gain_locked_ = std::abs(completion.gain - requested_gain_) <= 0.05F * requested_gain_ &&
                           std::abs(completion.exposure_us - config_.exposure_us) <= 100;
            gain_state_ = gain_locked_ ? "manual" : "waiting_controls";
            report = gain_locked_ || completion.timing.sensor_timestamp_ns >= next_manual_progress_;
            if (report) next_manual_progress_ = completion.timing.sensor_timestamp_ns + 500000000;
        }
        if (report) gain_progress(completion);
        if (gain_locked_) {
            sequence_ = 0;
            std::lock_guard<std::mutex> lock(state_mutex_);
            timing_.clear();
            camera_ready_ = true;
            timing_changed_.notify_all();
        }
    }

    void process(const Completion &completion) {
        if (!running_.load()) return;
        if (completion.startup) { requeue(completion.request); return; }
        const auto &timing = completion.timing;
        if (timing.sensor_timestamp_ns <= last_sensor_ || timing.timestamp_ns <= last_timestamp_)
            throw std::runtime_error("Camera sensor or common time stopped increasing");
        last_sensor_ = timing.sensor_timestamp_ns;
        last_timestamp_ = timing.timestamp_ns;
        if (!gain_locked_) {
            warmup(completion);
            requeue(completion.request);
            return;
        }
        if (std::abs(completion.gain - requested_gain_) > 0.05F * requested_gain_ ||
            std::abs(completion.exposure_us - config_.exposure_us) > 100)
            throw std::runtime_error("Camera exposure or gain changed after startup readiness");
        const auto sequence = sequence_++;
        actuator_->completed(completion.submission.phase, sequence, timing.timestamp_ns);
        {
            std::lock_guard<std::mutex> lock(state_mutex_);
            timing_.push_back(timing);
            while (!timing_.empty() && (timing_.size() > 512 || timing_.front().timestamp_ns < timing.timestamp_ns - 6 * second_ns)) timing_.pop_front();
            timing_changed_.notify_all();
        }
        auto storage = images_.acquire_storage();
        if (storage) {
            copy_pixels(completion.buffer, *storage);
            auto packet = std::make_shared<FramePacket>();
            packet->unit_id = config_.id;
            packet->sequence = sequence;
            packet->sensor_timestamp_ns = timing.sensor_timestamp_ns;
            packet->timestamp_ns = timing.timestamp_ns;
            packet->width = config_.width;
            packet->height = config_.height;
            packet->views = config_.views;
            packet->requested_exposure_us = completion.submission.exposure_us;
            packet->exposure_us = completion.exposure_us;
            packet->requested_gain = completion.submission.gain;
            packet->analogue_gain = completion.gain;
            packet->gain_locked = gain_locked_;
            packet->gain_state = gain_state_;
            packet->storage = std::move(storage);
            images_.publish(std::move(packet));
        }
        requeue(completion.request);
    }

    void copy_loop() noexcept {
        try {
            while (true) {
                Completion completion;
                {
                    std::unique_lock<std::mutex> lock(delivery_mutex_);
                    delivery_changed_.wait(lock, [&] { return delivery_stop_ || !deliveries_.empty(); });
                    if (delivery_stop_) return;
                    completion = deliveries_.front();
                    deliveries_.pop_front();
                }
                process(completion);
            }
        } catch (const std::exception &error) { fail(error.what()); }
        catch (...) { fail("Unknown camera copier failure"); }
    }

    void restore_before_stop() {
        if (!running_.load() || !copier_.joinable() || !actuator_) return;
        const auto state = actuator_->snapshot();
        if (state.state != "submitting" && state.state != "settling") return;
        actuator_->cancel(session_id_, round_id_);
        const auto budget = std::chrono::milliseconds(std::max<std::int64_t>(2000,
            static_cast<std::int64_t>(requests_.size() + 2) * (1000000 / config_.fps + 1000) / 1000 + 1000));
        std::unique_lock<std::mutex> lock(state_mutex_);
        const bool completed = timing_changed_.wait_for(lock, budget, [&] {
            return !fault_.empty() || !running_.load() || actuator_->snapshot().state == "restored";
        });
        if (!completed && fault_.empty()) fault_ = "Baseline frame period restoration timed out during stop";
    }

    void stop_locked(bool restore) {
        if (restore) {
            try { restore_before_stop(); }
            catch (const std::exception &error) { fail(error.what()); }
        }
        running_.store(false);
        {
            std::lock_guard<std::mutex> lock(delivery_mutex_);
            delivery_stop_ = true;
            delivery_changed_.notify_all();
        }
        images_.close();
        if (copier_.joinable()) copier_.join();
        if (camera_started_) camera_->stop();
        camera_started_ = false;
        if (connected_) camera_->requestCompleted.disconnect(this, &ArducamBackend::complete);
        connected_ = false;
        deliveries_.clear();
        requests_.clear();
        submitted_.clear();
        mappings_.clear();
        allocator_.reset();
        configuration_.reset();
        if (acquired_) camera_->release();
        acquired_ = false;
        camera_.reset();
        raw_ = nullptr;
        gain_warmup_.reset();
        metering_storage_.pixels.clear();
    }

public:
    ArducamBackend(UnitConfig config, std::shared_ptr<Manager> manager) : config_(std::move(config)), manager_(std::move(manager)) {}
    ~ArducamBackend() override { stop(); }

    void start(const std::string &session, std::uint64_t round, bool common_time) override {
        std::lock_guard<std::mutex> lifecycle(lifecycle_mutex_);
        if (camera_) throw std::runtime_error("Camera backend already started");
        actuator_ = std::make_unique<PhaseActuator>();
        actuator_->bind(session, round);
        session_id_ = session;
        round_id_ = round;
        common_time_ = common_time;
        sequence_ = 0;
        last_sensor_ = last_timestamp_ = 0;
        requested_gain_ = config_.auto_gain ? 1.0F : config_.analogue_gain;
        gain_locked_ = false;
        gain_state_ = "waiting_controls";
        next_manual_progress_ = 0;
        {
            std::lock_guard<std::mutex> lock(state_mutex_);
            fault_.clear();
            timing_.clear();
            camera_ready_ = false;
        }
        try {
            camera_ = manager_->camera_manager.get(config_.camera_id);
            if (!camera_) throw std::runtime_error("Camera ID not found: " + config_.camera_id);
            if (camera_->acquire()) throw std::runtime_error("Cannot acquire camera: " + config_.camera_id);
            acquired_ = true;
            configuration_ = camera_->generateConfiguration({libcamera::StreamRole::Viewfinder, libcamera::StreamRole::Raw});
            if (!configuration_ || configuration_->size() != 2) throw std::runtime_error("Camera requires viewfinder and raw streams");
            auto &viewfinder = configuration_->at(0);
            auto &raw = configuration_->at(1);
            viewfinder.size = {640, 480};
            viewfinder.pixelFormat = libcamera::formats::YUV420;
            viewfinder.bufferCount = 8;
            raw.size = {config_.width, config_.height};
            raw.pixelFormat = libcamera::formats::R8;
            raw.bufferCount = 8;
            configuration_->sensorConfig = libcamera::SensorConfiguration();
            configuration_->sensorConfig->outputSize = {config_.width, config_.height};
            configuration_->sensorConfig->bitDepth = 8;
            const auto format = raw.pixelFormat;
            if (configuration_->validate() == libcamera::CameraConfiguration::Invalid || raw.size.width != config_.width || raw.size.height != config_.height || raw.pixelFormat != format)
                throw std::runtime_error("Camera adjusted the required raw size or format");
            if (camera_->configure(configuration_.get())) throw std::runtime_error("Cannot configure camera");
            raw_ = raw.stream();
            stride_ = raw.stride;
            if (stride_ < config_.width) throw std::runtime_error("Raw camera stride is too short");
            const auto limits = camera_->controls().find(&libcamera::controls::FrameDurationLimits);
            if (limits == camera_->controls().end()) throw std::runtime_error("Camera does not provide frame-period control");
            minimum_duration_ = std::max<std::int64_t>(limits->second.min().get<std::int64_t>(), config_.exposure_us);
            maximum_duration_ = limits->second.max().get<std::int64_t>();
            const std::int64_t baseline = 1000000 / config_.fps;
            if (baseline < minimum_duration_ || baseline > maximum_duration_) throw std::runtime_error("Configured frame period is outside camera limits");
            const auto exposure_limits = camera_->controls().find(&libcamera::controls::ExposureTime);
            const auto gain_limits = camera_->controls().find(&libcamera::controls::AnalogueGain);
            if (exposure_limits == camera_->controls().end() || gain_limits == camera_->controls().end())
                throw std::runtime_error("Camera requires manual exposure and analogue-gain controls");
            if (config_.exposure_us < exposure_limits->second.min().get<std::int32_t>() ||
                config_.exposure_us > exposure_limits->second.max().get<std::int32_t>())
                throw std::runtime_error("Configured exposure is outside camera limits");
            const float gain_minimum = gain_limits->second.min().get<float>();
            const float gain_maximum = gain_limits->second.max().get<float>();
            if (!std::isfinite(gain_minimum) || !std::isfinite(gain_maximum) ||
                gain_minimum <= 0 || gain_minimum > gain_maximum ||
                requested_gain_ < gain_minimum || requested_gain_ > gain_maximum)
                throw std::runtime_error("Configured analogue gain is outside camera limits");
            if (config_.auto_gain) {
                gain_warmup_ = std::make_unique<detail::GainWarmup>(gain_minimum, gain_maximum);
                metering_storage_.pixels.resize(static_cast<std::size_t>(config_.width) * config_.height);
            }
            allocator_ = std::make_unique<libcamera::FrameBufferAllocator>(camera_);
            if (allocator_->allocate(viewfinder.stream()) < 0 || allocator_->allocate(raw_) < 0) throw std::runtime_error("Cannot allocate camera buffers");
            const auto &raw_buffers = allocator_->buffers(raw_);
            const auto &viewfinder_buffers = allocator_->buffers(viewfinder.stream());
            if (raw_buffers.empty() || raw_buffers.size() != viewfinder_buffers.size()) throw std::runtime_error("Camera stream buffer counts differ");
            const long page = sysconf(_SC_PAGESIZE);
            if (page <= 0) throw std::runtime_error("Cannot query page size");
            for (std::size_t i = 0; i < raw_buffers.size(); ++i) {
                auto *buffer = raw_buffers[i].get();
                if (buffer->planes().size() != 1) throw std::runtime_error("Raw camera buffer must contain one plane");
                const auto &plane = buffer->planes().front();
                if (plane.offset == libcamera::FrameBuffer::Plane::kInvalidOffset || plane.length < static_cast<std::size_t>(stride_) * config_.height)
                    throw std::runtime_error("Raw camera plane has invalid extent");
                mappings_.emplace(buffer, std::make_unique<Mapping>(plane, page));
                auto request = camera_->createRequest();
                if (!request || request->addBuffer(raw_, buffer) < 0 || request->addBuffer(viewfinder.stream(), viewfinder_buffers[i].get()) < 0)
                    throw std::runtime_error("Cannot create camera request");
                submitted_.emplace(request.get(), Submission{{baseline, 0, false, round}, config_.exposure_us, requested_gain_});
                const std::int64_t initial_duration[]{baseline, baseline};
                request->controls().set(libcamera::controls::FrameDurationLimits, libcamera::Span<const std::int64_t, 2>(initial_duration));
                request->controls().set(libcamera::controls::ExposureTime, config_.exposure_us);
                request->controls().set(libcamera::controls::AnalogueGain, requested_gain_);
                requests_.push_back(std::move(request));
            }
            images_.open(static_cast<std::size_t>(config_.width) * config_.height, config_.queue_capacity, config_.pool_capacity);
            libcamera::ControlList controls(camera_->controls());
            const std::int64_t durations[]{baseline, baseline};
            controls.set(libcamera::controls::FrameDurationLimits, libcamera::Span<const std::int64_t, 2>(durations));
            controls.set(libcamera::controls::AeEnable, false);
            controls.set(libcamera::controls::ExposureTime, config_.exposure_us);
            controls.set(libcamera::controls::AnalogueGain, requested_gain_);
            camera_->requestCompleted.connect(this, &ArducamBackend::complete);
            connected_ = true;
            if (camera_->start(&controls)) throw std::runtime_error("Cannot start camera");
            camera_started_ = true;
            {
                std::lock_guard<std::mutex> lock(delivery_mutex_);
                delivery_stop_ = false;
            }
            running_.store(true);
            copier_ = std::thread([this] { copy_loop(); });
            for (const auto &request : requests_) if (camera_->queueRequest(request.get()) < 0) throw std::runtime_error("Initial camera request submission failed");
            {
                std::unique_lock<std::mutex> lock(state_mutex_);
                const bool settled = timing_changed_.wait_for(lock, std::chrono::seconds(20), [&] {
                    return camera_ready_ || !fault_.empty() || !running_.load();
                });
                if (!fault_.empty()) throw std::runtime_error(fault_);
                if (!settled || !camera_ready_) throw std::runtime_error("Camera controls did not settle within 20 seconds");
            }
        } catch (...) { stop_locked(false); throw; }
    }

    void stop() override {
        std::lock_guard<std::mutex> lifecycle(lifecycle_mutex_);
        stop_locked(true);
    }
    TimingSnapshot snapshot() const override {
        std::lock_guard<std::mutex> lifecycle(lifecycle_mutex_);
        TimingSnapshot result;
        result.unit_id = config_.id;
        result.snapshot_timestamp_ns = domain_now();
        {
            std::lock_guard<std::mutex> lock(state_mutex_);
            result.frames.assign(timing_.begin(), timing_.end());
        }
        if (actuator_) result.correction = correction_status(actuator_->snapshot());
        return result;
    }
    CorrectionStatus adjust(const Pulse &pulse) override {
        std::lock_guard<std::mutex> lifecycle(lifecycle_mutex_);
        if (!running_.load()) throw std::runtime_error("Cannot adjust a stopped camera");
        return correction_status(actuator_->adjust(session_id_, round_id_, pulse.correction_id, pulse.delta_us, pulse.count, pulse.expires_ns,
                                                   domain_now(), monotonic_ns(), 1000000 / config_.fps, minimum_duration_, maximum_duration_));
    }
    void cancel() override {
        std::lock_guard<std::mutex> lifecycle(lifecycle_mutex_);
        if (actuator_) actuator_->cancel(session_id_, round_id_);
    }
    std::shared_ptr<const FramePacket> read(std::chrono::milliseconds timeout) override {
        return images_.read(timeout);
    }
    std::string fault() const override {
        std::lock_guard<std::mutex> lock(state_mutex_);
        return fault_;
    }
};
}

std::vector<std::unique_ptr<AcquisitionBackend>> make_arducam_backends(const std::vector<UnitConfig> &units) {
    if (units.empty()) return {};
    for (const auto &unit : units) {
        profile_pipeline(unit.profile);
        if (unit.width != 5120 || unit.height != 800 || unit.views != 4)
            throw std::invalid_argument("Arducam B0267 Pi4 profile requires 5120x800 with four views");
        if (unit.auto_gain && unit.analogue_gain != 1.0F)
            throw std::invalid_argument("Startup analogue-gain calibration must start at 1x");
    }
    auto manager = std::make_shared<Manager>(profile_pipeline(units.front().profile));
    std::vector<std::unique_ptr<AcquisitionBackend>> result;
    result.reserve(units.size());
    for (const auto &unit : units) result.push_back(std::make_unique<ArducamBackend>(unit, manager));
    return result;
}

std::vector<std::string> enumerate_arducam_cameras(const std::string &profile) {
    Manager manager(profile_pipeline(profile));
    const auto cameras = manager.camera_manager.cameras();
    std::vector<std::string> ids;
    ids.reserve(cameras.size());
    for (const auto &camera : cameras) ids.push_back(camera->id());
    return ids;
}
}
