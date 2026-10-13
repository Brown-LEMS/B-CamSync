#include "bcamsync/frame_queue.hpp"
#include <algorithm>
#include <atomic>
#include <future>
#include <iostream>
#include <thread>

using namespace bcamsync;
using namespace std::chrono_literals;
namespace {
void require(bool condition, const char *message) { if (!condition) throw std::runtime_error(message); }
template<class F> void rejects(F action) {
    bool rejected = false;
    try { action(); } catch (const std::exception &) { rejected = true; }
    require(rejected, "Expected rejection");
}
bool emit(FrameQueue &queue, std::uint64_t sequence) {
    auto storage = queue.acquire_storage();
    if (!storage) return false;
    std::fill(storage->pixels.begin(), storage->pixels.end(), static_cast<std::uint8_t>(sequence % 251));
    auto packet = std::make_shared<FramePacket>();
    packet->unit_id = "board";
    packet->sequence = sequence;
    packet->width = 8;
    packet->height = 2;
    packet->views = 4;
    packet->storage = std::move(storage);
    queue.publish(std::move(packet));
    return true;
}
void intact(const std::shared_ptr<const FramePacket> &packet) {
    require(packet && packet->storage && packet->storage->pixels.size() == 16, "Frame storage extent");
    const auto expected = static_cast<std::uint8_t>(packet->sequence % 251);
    require(std::all_of(packet->storage->pixels.begin(), packet->storage->pixels.end(),
        [&](std::uint8_t value) { return value == expected; }), "Retained frame pixels changed");
}
void eviction_and_retention() {
    FrameQueue queue;
    queue.open(16, 2, 4);
    require(!queue.read(0ms), "Empty queue timeout");
    emit(queue, 10); emit(queue, 11); emit(queue, 12);
    auto first = queue.read(0ms), second = queue.read(0ms);
    require(first && first->sequence == 11 && second && second->sequence == 12, "Queue keeps newest frames in capture order");
    require(first->dropped_before == 1 && second->dropped_before == 1, "Queue eviction is cumulative at delivery");
    for (std::uint64_t i = 13; i < 1000; ++i) {
        require(emit(queue, i), "Retained frames must not prevent reuse of free pool storage");
        intact(queue.read(0ms));
        intact(first); intact(second);
    }
    std::shared_ptr<const std::uint8_t> view(first->storage, first->storage->pixels.data() + 2);
    first.reset();
    queue.close();
    require(*view == 11, "View ownership survives packet release and queue close");
    intact(second);
    require(!queue.read(0ms), "Closed queue must not deliver stale packets");
    queue.open(16, 2, 4);
    emit(queue, 1001);
    require(queue.read(0ms)->dropped_before == 0, "New session resets drop counters");
    require(*view == 11, "New session cannot overwrite old retained storage");
    queue.close();
}
void retained_pool_exhaustion() {
    FrameQueue queue;
    queue.open(16, 1, 2);
    emit(queue, 20); auto first = queue.read(0ms);
    emit(queue, 21); auto second = queue.read(0ms);
    for (std::uint64_t i = 22; i < 42; ++i) require(!emit(queue, i), "Fully retained pool must drop incoming images without blocking");
    intact(first); intact(second);
    first.reset();
    require(emit(queue, 42), "Released storage resumes image delivery");
    auto resumed = queue.read(0ms);
    require(resumed && resumed->sequence == 42 && resumed->dropped_before == 20, "Pool-exhaustion losses remain visible");
    intact(second); intact(resumed);
    queue.close();
}
void wakeups_and_faults() {
    FrameQueue queue;
    rejects([&] { queue.open(0, 1, 2); });
    rejects([&] { queue.open(16, 1, 1); });
    queue.open(16, 1, 2);
    rejects([&] { queue.open(16, 1, 2); });
    rejects([&] { queue.publish({}); });
    std::promise<void> started;
    auto reader = std::async(std::launch::async, [&] { started.set_value(); return queue.read(5s); });
    started.get_future().wait();
    queue.close();
    require(reader.wait_for(1s) == std::future_status::ready && !reader.get(), "Close wakes a blocked reader");
    queue.open(16, 1, 2);
    std::promise<void> waiting;
    auto failing_reader = std::async(std::launch::async, [&] {
        waiting.set_value();
        try { queue.read(5s); } catch (const std::runtime_error &error) { return std::string(error.what()); }
        return std::string{};
    });
    waiting.get_future().wait();
    queue.fail("capture failed");
    require(failing_reader.wait_for(1s) == std::future_status::ready && failing_reader.get() == "capture failed", "Fault wakes and informs a blocked reader");
    queue.close();
    queue.open(16, 1, 2);
    emit(queue, 4); intact(queue.read(0ms));
    queue.close();
}
void concurrent_capture() {
    FrameQueue queue;
    queue.open(16, 2, 6);
    std::atomic<bool> finished{false};
    auto producer = std::async(std::launch::async, [&] {
        for (std::uint64_t i = 0; i < 20000; ++i) emit(queue, i);
        finished.store(true);
    });
    std::deque<std::shared_ptr<const FramePacket>> retained;
    std::uint64_t last = 0;
    bool observed = false;
    while (true) {
        auto packet = queue.read(1ms);
        if (!packet) {
            if (finished.load()) break;
            continue;
        }
        require(!observed || packet->sequence > last, "Concurrent delivery preserves sequence order");
        last = packet->sequence;
        observed = true;
        retained.push_back(std::move(packet));
        if (retained.size() > 3) retained.pop_front();
        for (const auto &frame : retained) intact(frame);
    }
    producer.get();
    require(observed, "Concurrent producer delivered frames");
    queue.close();
    for (const auto &frame : retained) intact(frame);
}
}
int main() {
    try {
        eviction_and_retention(); retained_pool_exhaustion(); wakeups_and_faults(); concurrent_capture();
        std::cout << "Frame queue tests passed\n";
    } catch (const std::exception &error) { std::cerr << error.what() << '\n'; return 1; }
}
