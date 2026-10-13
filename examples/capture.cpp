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
        std::cout << "Consuming local board packets; synchronization status is reported separately\n" << std::flush;
        const auto end = std::chrono::steady_clock::now() + std::chrono::duration<double>(seconds);
        const auto shutdown_deadline = end + std::chrono::duration<double>(config.startup_timeout_seconds + 10);
        auto next_report = std::chrono::steady_clock::now();
        while (true) {
            const auto status = session.status();
            if (status.state == "stopped") {
                if (coordinator) throw std::runtime_error("Coordinator stopped unexpectedly");
                break;
            }
            if (coordinator && std::chrono::steady_clock::now() >= end) break;
            if (!coordinator && std::chrono::steady_clock::now() >= shutdown_deadline)
                throw std::runtime_error("Coordinator shutdown timed out");
            for (const auto &unit : config.units) {
                if (unit.node_id != node) continue;
                const auto frame = session.read(unit.id, std::chrono::seconds(1));
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
                              << ", dropped=" << frame->dropped_before
                              << ", clock_valid=" << frame->clock_valid << ", sync=" << status.state << ", fault=" << status.fault << '\n' << std::flush;
                    next_report = std::chrono::steady_clock::now() + std::chrono::seconds(1);
                }
            }
        }
        session.stop();
        const auto closed = session.status();
        if (!closed.fault.empty()) std::cerr << "Synchronization fault: " << closed.fault << '\n';
    } catch (const std::exception &error) {
        std::cerr << "B-CamSync: " << error.what() << '\n';
        return 1;
    }
}
