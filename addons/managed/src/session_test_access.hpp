#pragma once
#include "bcamsync/session.hpp"
#include <utility>

namespace bcamsync::detail {
/// Internal software-test construction; physical clock requirements follow Config.backend.
class SessionTestAccess {
public:
    static Session create(Config config, std::string node,
                          std::vector<std::unique_ptr<AcquisitionBackend>> backends) {
        return Session(std::move(config), std::move(node), std::move(backends));
    }
    static std::unique_ptr<Session> make(Config config, std::string node,
                                       std::vector<std::unique_ptr<AcquisitionBackend>> backends) {
        return std::unique_ptr<Session>(new Session(std::move(config), std::move(node), std::move(backends)));
    }
};
} // namespace bcamsync::detail
