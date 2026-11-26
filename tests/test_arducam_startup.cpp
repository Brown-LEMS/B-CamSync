#include "arducam_startup.hpp"
#include <iostream>
#include <stdexcept>

using Startup = bcamsync::detail::ArducamStartup;
using namespace std::chrono_literals;

int main() {
    try {
        Startup startup;
        const auto start = Startup::Time{};
        const auto deadline = start + 20s;
        const auto outcome = [&](Startup::Time now) { return startup.result(now, deadline, 1500ms, 3); };
        const auto expect = [](bool ok, const char *why) { if (!ok) throw std::runtime_error(why); };

        expect(outcome(start + 1500ms) == Startup::Result::no_frames, "A silent stream must time out");
        startup.observe(start + 40ms, 10, true);
        expect(outcome(start + 1500ms) == Startup::Result::waiting, "A real frame advances the silence deadline");
        expect(outcome(start + 1540ms) == Startup::Result::no_frames, "A stream stalling after one good frame must time out");
        startup.observe(start + 65ms, 11, false);
        startup.observe(start + 90ms, 12, true);
        startup.observe(start + 115ms, 13, true);
        expect(outcome(start + 115ms) == Startup::Result::waiting, "Controls must match for consecutive frames");
        startup.observe(start + 140ms, 14, true);
        expect(outcome(start + 140ms) == Startup::Result::ready, "Three stable frames establish readiness");
        expect(outcome(start + 1640ms) == Startup::Result::no_frames, "Stale frames cannot establish readiness");
        startup.cancelled = true;
        expect(outcome(start + 141ms) == Startup::Result::cancelled, "A pending cancellation takes precedence over readiness");
        startup = {};
        startup.observe(deadline, 0, false);
        expect(outcome(deadline) == Startup::Result::controls_timeout, "Live but unsettled controls must fail without a restart");
        expect(startup.valid_frames == 1, "Restart clears validity and cancellation evidence");
        startup = {};
        startup.observe(start + 25ms, 5, true);
        startup.observe(start + 50ms, 7, true);
        startup.observe(start + 75ms, 8, true);
        expect(outcome(start + 75ms) == Startup::Result::waiting, "A gap in the camera sequence resets the readiness window");
        startup.invalid_frame();
        startup.observe(start + 100ms, 9, true);
        expect(startup.stable_frames == 1, "An invalid frame resets the readiness window");
        startup.observe(start + 125ms, UINT32_MAX - 1, true);
        startup.observe(start + 150ms, UINT32_MAX, true);
        startup.observe(start + 175ms, 0, true);
        expect(outcome(start + 175ms) == Startup::Result::ready, "The uint32 camera sequence wraps continuously");
        std::cout << "Arducam startup tests passed\n";
    } catch (const std::exception &error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
