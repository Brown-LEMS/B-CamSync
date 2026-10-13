#pragma once

#include "types.hpp"
#include <chrono>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <stdexcept>
#include <utility>

namespace bcamsync {
/// Bounded native image delivery; exhausted storage drops images instead of blocking capture.
class FrameQueue {
    std::mutex mutex_;
    std::condition_variable changed_;
    std::vector<std::shared_ptr<FrameStorage>> pool_;
    std::deque<std::shared_ptr<const FramePacket>> packets_;
    std::size_t capacity_ = 0;
    std::uint64_t dropped_ = 0;
    bool closed_ = true;
    std::string fault_;
public:
    void open(std::size_t pixels, unsigned queue_capacity, unsigned pool_capacity) {
        if (!pixels || !queue_capacity || pool_capacity <= queue_capacity)
            throw std::invalid_argument("Invalid image queue or storage capacity");
        std::vector<std::shared_ptr<FrameStorage>> pool;
        pool.reserve(pool_capacity);
        for (unsigned i = 0; i < pool_capacity; ++i) {
            auto storage = std::make_shared<FrameStorage>();
            storage->pixels.resize(pixels);
            pool.push_back(std::move(storage));
        }
        std::lock_guard<std::mutex> lock(mutex_);
        if (!closed_) throw std::logic_error("Image queue is already open");
        pool_ = std::move(pool);
        packets_.clear();
        capacity_ = queue_capacity;
        dropped_ = 0;
        fault_.clear();
        closed_ = false;
    }

    /// Only the capture producer writes acquired storage, then relinquishes it to publish().
    std::shared_ptr<FrameStorage> acquire_storage() {
        std::lock_guard<std::mutex> lock(mutex_);
        if (closed_) return {};
        for (;;) {
            for (const auto &storage : pool_) if (storage.use_count() == 1) return storage;
            if (packets_.empty()) {
                ++dropped_;
                return {};
            }
            packets_.pop_front();
            ++dropped_;
        }
    }

    void publish(std::shared_ptr<const FramePacket> packet) {
        if (!packet || !packet->storage) throw std::invalid_argument("Cannot deliver an empty image packet");
        std::lock_guard<std::mutex> lock(mutex_);
        if (closed_) return;
        if (packets_.size() == capacity_) {
            packets_.pop_front();
            ++dropped_;
        }
        packets_.push_back(std::move(packet));
        changed_.notify_one();
    }

    /// dropped_before is the cumulative count of queue evictions and unbuffered captures at delivery.
    std::shared_ptr<const FramePacket> read(std::chrono::milliseconds timeout) {
        std::unique_lock<std::mutex> lock(mutex_);
        changed_.wait_for(lock, timeout, [&] { return !packets_.empty() || !fault_.empty() || closed_; });
        if (!fault_.empty()) throw std::runtime_error(fault_);
        if (packets_.empty()) return {};
        auto packet = std::make_shared<FramePacket>(*packets_.front());
        packets_.pop_front();
        packet->dropped_before = dropped_;
        return packet;
    }

    void fail(const std::string &message) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (fault_.empty()) fault_ = message;
        changed_.notify_all();
    }

    /// Retained packets own their storage independently and remain valid after close().
    void close() {
        std::lock_guard<std::mutex> lock(mutex_);
        closed_ = true;
        packets_.clear();
        pool_.clear();
        changed_.notify_all();
    }
};
}
