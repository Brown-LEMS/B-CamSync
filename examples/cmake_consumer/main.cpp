#include <bcamsync/session.hpp>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <vector>

int main(int argc, char **argv) {
    if (argc != 2) {
        std::cerr << "Usage: consumer FAKE_CONFIGURATION\n";
        return 2;
    }
    try {
        const auto config = bcamsync::Config::from_file(argv[1]);
        if (config.backend != "fake" || config.nodes.size() != 1 || config.units.size() != 2)
            throw std::invalid_argument("Installed-package check requires one fake node with two boards");
        const auto node = config.reference_node();
        bcamsync::Session session(config, node);
        std::vector<std::shared_ptr<const bcamsync::FramePacket>> retained;
        std::vector<std::vector<std::uint8_t>> originals;
        std::cout << "Checking installed C++ package with a public fake session\n" << std::flush;
        session.start();
        if (!session.wait_ready(std::chrono::seconds(45)))
            throw std::runtime_error("Fake synchronization readiness timed out");
        for (const auto &unit : config.units) {
            if (unit.node_id != node) continue;
            auto packet = session.read(unit.id, std::chrono::seconds(1));
            if (!packet || !packet->storage || packet->unit_id != unit.id || packet->views != unit.views ||
                packet->width != unit.width || packet->height != unit.height ||
                packet->storage->pixels.size() != static_cast<std::size_t>(unit.width) * unit.height)
                throw std::runtime_error("Invalid board packet from installed library");
            for (unsigned view = 0; view < packet->views; ++view)
                (void)packet->view_data(view);
            originals.push_back(packet->storage->pixels);
            retained.push_back(std::move(packet));
        }
        for (const auto &packet : retained) {
            const auto next = session.read(packet->unit_id, std::chrono::seconds(1));
            if (!next || next->sequence <= packet->sequence)
                throw std::runtime_error("Installed library did not continue acquisition");
        }
        session.stop();
        const auto status = session.status();
        if (!status.fault.empty() || status.ready || status.state != "stopped")
            throw std::runtime_error("Installed library did not shut down cleanly: " + status.fault);
        for (std::size_t index = 0; index < retained.size(); ++index)
            if (retained[index]->storage->pixels != originals[index])
                throw std::runtime_error("Retained pixels changed after session shutdown");
        std::cout << "Installed C++ package acquired both boards and retained their pixels after stop\n";
    } catch (const std::exception &error) {
        std::cerr << "Installed-package check: " << error.what() << '\n';
        return 1;
    }
}
