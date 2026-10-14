#pragma once
#include "bcamsync/sync_types.hpp"
#include <functional>
#include <memory>
#include <nlohmann/json.hpp>
namespace bcamsync::detail {
using Json = nlohmann::json;
Json snapshot_json(const TimingSnapshot& s);
TimingSnapshot snapshot_from_json(const Json& j);
Json topology_json(const SyncConfig& c);
Json status_json(const GroupStatus& s);
GroupStatus status_from_json(const Json& j);
class Connection {
  public:
    Connection(int fd, double timeout);
    ~Connection();
    Connection(const Connection&) = delete;
    void send(const Json& j);
    Json receive();
    void shutdown();
    void timeout(double seconds) { timeout_ = seconds; }

  private:
    int fd_;
    double timeout_;
    std::string pending_;
};
int listener(const NodeConfig& node);
std::unique_ptr<Connection> connect(const NodeConfig& node, double timeout);
} // namespace bcamsync::detail
