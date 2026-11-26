#include <bcamsync/session.hpp>
#include <chrono>
#include <cmath>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

int main(int argc, char **argv) {
    bool verbose = false;
    std::vector<std::string> positional;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "-v" || arg == "--verbose") verbose = true;
        else positional.push_back(arg);
    }
    if (positional.size() < 2 || positional.size() > 3) {
        std::cerr << "Usage: bcamsync_capture [-v|--verbose] CONFIG NODE [SECONDS]\n";
        return 2;
    }
    try {
        bcamsync::configure_camera_console(verbose);
        const auto config = bcamsync::Config::from_file(positional[0]);
        const auto &node = positional[1];
        const double seconds = positional.size() == 3 ? std::stod(positional[2]) : 10.0;
        if (!std::isfinite(seconds) || seconds <= 0) throw std::invalid_argument("SECONDS must be finite and positive");
        bcamsync::Session session(config, node);
        session.start();
        std::cout << "Consuming local board packets; synchronization status is reported separately\n" << std::flush;
        const auto end = std::chrono::steady_clock::now() + std::chrono::duration<double>(seconds);
        auto next_report = std::chrono::steady_clock::now();
        while (std::chrono::steady_clock::now() < end) {
            const auto status = session.status();
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
                if (verbose && std::chrono::steady_clock::now() >= next_report) {
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
