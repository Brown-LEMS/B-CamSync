#include "bcamsync/actuator.hpp"
#include "bcamsync/backend.hpp"
#include "bcamsync/frame_queue.hpp"

#include <libcamera/camera.h>
#include <libcamera/camera_manager.h>
#include <libcamera/control_ids.h>
#include <libcamera/controls.h>
#include <libcamera/formats.h>
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
#include <cstdlib>
#include <cstring>
#include <deque>
#include <map>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <utility>

namespace bcamsync {
namespace {
constexpr std::int64_t second_ns = 1000000000LL;

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
    std::map<libcamera::Request *, PhaseActuator::Submission> submitted_;
    libcamera::Stream *raw_ = nullptr;
    unsigned stride_ = 0;
    std::int64_t minimum_duration_ = 0, maximum_duration_ = 0;
    bool acquired_ = false, camera_started_ = false, connected_ = false, common_time_ = false;
    std::atomic<bool> running_{false};
    mutable std::mutex lifecycle_mutex_;
    std::unique_ptr<PhaseActuator> actuator_;
    std::string session_id_;
    std::uint64_t round_id_ = 0, sequence_ = 0;
    std::int64_t last_sensor_ = 0, last_timestamp_ = 0;

    mutable std::mutex state_mutex_;
    std::condition_variable timing_changed_;
    std::deque<FrameTiming> timing_;
    FrameQueue images_;
    std::string fault_;

    std::mutex delivery_mutex_;
    std::condition_variable delivery_changed_;
    std::deque<libcamera::Request *> deliveries_;
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
            std::lock_guard<std::mutex> lock(delivery_mutex_);
            if (delivery_stop_) return;
            deliveries_.push_back(request);
            delivery_changed_.notify_one();
        } catch (const std::exception &error) { fail(error.what()); }
        catch (...) { fail("Unknown camera completion callback failure"); }
    }

    void requeue(libcamera::Request *request) {
        if (!running_.load()) return;
        request->reuse(libcamera::Request::ReuseBuffers);
        const auto control = actuator_->next(1000000 / config_.fps, monotonic_ns());
        submitted_.at(request) = control;
        const std::int64_t limits[]{control.duration_us, control.duration_us};
        request->controls().set(libcamera::controls::FrameDurationLimits, libcamera::Span<const std::int64_t, 2>(limits));
        if (camera_->queueRequest(request) < 0) throw std::runtime_error("Camera request submission failed");
    }

    void process(libcamera::Request *request) {
        if (!running_.load()) return;
        if (request->status() != libcamera::Request::RequestComplete) throw std::runtime_error("Incomplete camera request");
        auto *buffer = request->buffers().at(raw_);
        const auto &metadata = buffer->metadata();
        if (metadata.status == libcamera::FrameMetadata::FrameStartup) { requeue(request); return; }
        if (metadata.status != libcamera::FrameMetadata::FrameSuccess) throw std::runtime_error("Unsuccessful camera raw buffer");
        const auto sensor = request->metadata().get(libcamera::controls::SensorTimestamp);
        const auto duration = request->metadata().get(libcamera::controls::FrameDuration);
        if (!sensor || !duration) throw std::runtime_error("Camera requires SensorTimestamp and FrameDuration metadata");
        const auto submission = submitted_.at(request);
        FrameTiming timing;
        timing.camera_sequence = metadata.sequence;
        timing.sensor_timestamp_ns = *sensor;
        timing.timestamp_ns = *sensor;
        if (common_time_) {
            const auto before = boot_ns(), real = realtime_ns(), after = boot_ns();
            timing.mapping_span_ns = after - before;
            timing.timestamp_ns += real - (before + (after - before) / 2);
        }
        if (timing.sensor_timestamp_ns <= last_sensor_ || timing.timestamp_ns <= last_timestamp_)
            throw std::runtime_error("Camera sensor or common time stopped increasing");
        last_sensor_ = timing.sensor_timestamp_ns;
        last_timestamp_ = timing.timestamp_ns;
        timing.requested_duration_us = submission.duration_us;
        timing.reported_duration_us = *duration;
        timing.correction_id = submission.correction_id;
        const auto sequence = sequence_++;
        actuator_->completed(submission, sequence, timing.timestamp_ns);
        {
            std::lock_guard<std::mutex> lock(state_mutex_);
            timing_.push_back(timing);
            while (!timing_.empty() && (timing_.size() > 512 || timing_.front().timestamp_ns < timing.timestamp_ns - 6 * second_ns)) timing_.pop_front();
            timing_changed_.notify_all();
        }
        auto storage = images_.acquire_storage();
        if (storage) {
            DmaRead access(buffer->planes().front().fd.get());
            const auto *source = mappings_.at(buffer)->pixels;
            if (config_.raw_format == "R8" && stride_ == config_.width)
                std::memcpy(storage->pixels.data(), source, storage->pixels.size());
            else for (unsigned y = 0; y < config_.height; ++y) {
                const auto *row = source + static_cast<std::size_t>(y) * stride_;
                auto *out = storage->pixels.data() + static_cast<std::size_t>(y) * config_.width;
                if (config_.raw_format == "R8") std::memcpy(out, row, config_.width);
                else for (unsigned x = 0; x < config_.width; ++x) {
                    const unsigned value = row[2 * x] | (static_cast<unsigned>(row[2 * x + 1]) << 8);
                    out[x] = static_cast<std::uint8_t>(std::min(255U, value >> config_.r16_shift));
                }
            }
            access.finish();
            auto packet = std::make_shared<FramePacket>();
            packet->unit_id = config_.id;
            packet->sequence = sequence;
            packet->sensor_timestamp_ns = timing.sensor_timestamp_ns;
            packet->timestamp_ns = timing.timestamp_ns;
            packet->width = config_.width;
            packet->height = config_.height;
            packet->views = config_.views;
            packet->storage = std::move(storage);
            images_.publish(std::move(packet));
        }
        requeue(request);
    }

    void copy_loop() noexcept {
        try {
            while (true) {
                libcamera::Request *request;
                {
                    std::unique_lock<std::mutex> lock(delivery_mutex_);
                    delivery_changed_.wait(lock, [&] { return delivery_stop_ || !deliveries_.empty(); });
                    if (delivery_stop_) return;
                    request = deliveries_.front();
                    deliveries_.pop_front();
                }
                process(request);
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
        {
            std::lock_guard<std::mutex> lock(state_mutex_);
            fault_.clear();
            timing_.clear();
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
            raw.pixelFormat = config_.raw_format == "R8" ? libcamera::formats::R8 : libcamera::formats::R16;
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
            if (stride_ < config_.width * (config_.raw_format == "R8" ? 1U : 2U)) throw std::runtime_error("Raw camera stride is too short");
            const auto limits = camera_->controls().find(&libcamera::controls::FrameDurationLimits);
            if (limits == camera_->controls().end()) throw std::runtime_error("Camera does not provide frame-period control");
            minimum_duration_ = std::max<std::int64_t>(limits->second.min().get<std::int64_t>(), config_.exposure_us);
            maximum_duration_ = limits->second.max().get<std::int64_t>();
            const std::int64_t baseline = 1000000 / config_.fps;
            if (baseline < minimum_duration_ || baseline > maximum_duration_) throw std::runtime_error("Configured frame period is outside camera limits");
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
                submitted_.emplace(request.get(), PhaseActuator::Submission{baseline, 0, false, round});
                requests_.push_back(std::move(request));
            }
            images_.open(static_cast<std::size_t>(config_.width) * config_.height, config_.queue_capacity, config_.pool_capacity);
            libcamera::ControlList controls(camera_->controls());
            const std::int64_t durations[]{baseline, baseline};
            controls.set(libcamera::controls::FrameDurationLimits, libcamera::Span<const std::int64_t, 2>(durations));
            controls.set(libcamera::controls::AeEnable, false);
            controls.set(libcamera::controls::ExposureTime, config_.exposure_us);
            controls.set(libcamera::controls::AnalogueGain, config_.analogue_gain);
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
    const auto &pipeline = units.front().pipeline_config;
    for (const auto &unit : units) if (unit.pipeline_config != pipeline)
        throw std::invalid_argument("All local Arducam units must share one pipeline configuration");
    auto manager = std::make_shared<Manager>(pipeline);
    std::vector<std::unique_ptr<AcquisitionBackend>> result;
    result.reserve(units.size());
    for (const auto &unit : units) result.push_back(std::make_unique<ArducamBackend>(unit, manager));
    return result;
}
}
