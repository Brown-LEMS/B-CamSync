#include <bcamsync/session.hpp>
#include <chrono>
#include <cmath>
#include <iostream>
#include <stdexcept>
#include <string>

int main(int argc, char **argv) {
    if (argc < 3 || argc > 4) {
        std::cerr << "Usage: bcamsync_capture CONFIG NODE [SECONDS]\n";
        return 2;
    }
    try {
        const auto config = bcamsync::Config::from_file(argv[1]);
        const std::string node = argv[2];
        const bool coordinator = config.reference_node() == node;
        const double seconds = argc == 4 ? std::stod(argv[3]) : 10.0;
        if (!std::isfinite(seconds) || seconds <= 0) throw std::invalid_argument("SECONDS must be finite and positive");
        bcamsync::Session session(config, node);
        session.start();
        if (!session.wait_ready(std::chrono::milliseconds(static_cast<std::int64_t>((config.startup_timeout_seconds + 10) * 1000))))
            throw std::runtime_error("Synchronization readiness timed out");
        std::cout << "Synchronization ready; consuming local board packets\n" << std::flush;
        const auto end = std::chrono::steady_clock::now() + std::chrono::duration<double>(seconds);
        const auto shutdown_deadline = end + std::chrono::duration<double>(config.startup_timeout_seconds + 10);
        auto next_report = std::chrono::steady_clock::now();
        while (true) {
            const auto status = session.status();
            if (!status.fault.empty()) throw std::runtime_error(status.fault);
            if (status.state == "stopped") {
                if (coordinator) throw std::runtime_error("Coordinator stopped unexpectedly");
                break;
            }
            if (!status.ready) throw std::runtime_error("Synchronization readiness was lost");
            if (coordinator && std::chrono::steady_clock::now() >= end) break;
            if (!coordinator && std::chrono::steady_clock::now() >= shutdown_deadline)
                throw std::runtime_error("Coordinator shutdown timed out");
            for (const auto &unit : config.units) {
                if (unit.node_id != node) continue;
                std::shared_ptr<const bcamsync::FramePacket> frame;
                try {
                    frame = session.read(unit.id, std::chrono::seconds(1));
                } catch (const std::runtime_error &) {
                    const auto current = session.status();
                    if (!coordinator && current.state == "stopped" && current.fault.empty()) break;
                    throw;
                }
                if (!frame) continue;
                // Retain frame while accessing its four strided camera views.
                for (unsigned view = 0; view < frame->views; ++view) {
                    const auto *pixels = frame->view_data(view);
                    const auto row_stride = frame->width;
                    (void)pixels;
                    (void)row_stride;
                }
                if (std::chrono::steady_clock::now() >= next_report) {
                    std::cout << frame->unit_id << ": sequence=" << frame->sequence
                              << ", views=" << frame->views << " x " << frame->view_width() << "x" << frame->height
                              << ", dropped=" << frame->dropped_before << '\n' << std::flush;
                    next_report = std::chrono::steady_clock::now() + std::chrono::seconds(1);
                }
            }
        }
        session.stop();
        const auto closed = session.status();
        if (!closed.fault.empty() || closed.state != "stopped" || closed.ready)
            throw std::runtime_error(closed.fault.empty() ? "Session did not close cleanly" : closed.fault);
    } catch (const std::exception &error) {
        std::cerr << "B-CamSync: " << error.what() << '\n';
        return 1;
    }
}
