#include "process.hpp"
#include "bcamsync/types.hpp"
#include <cerrno>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <spawn.h>
#include <stdexcept>
#include <sys/wait.h>
#include <unistd.h>
extern char** environ;
namespace bcamsync::detail {
std::string run_program(const std::vector<std::string>& args, double timeout) {
    if (args.empty())
        throw std::invalid_argument("empty command");
    std::vector<char*> argv;
    for (const auto& a : args)
        argv.push_back(const_cast<char*>(a.c_str()));
    argv.push_back(nullptr);
    std::vector<std::string> environment;
    for (auto p = environ; *p; ++p) {
        const std::string value(*p);
        if (value.compare(0, 7, "LC_ALL=") != 0)
            environment.push_back(value);
    }
    environment.emplace_back("LC_ALL=C");
    std::vector<char*> env;
    for (auto& entry : environment)
        env.push_back(entry.data());
    env.push_back(nullptr);
    int fds[2];
    if (pipe(fds))
        throw std::runtime_error("cannot create command pipe");
    for (int fd : fds)
        fcntl(fd, F_SETFD, FD_CLOEXEC);
    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_adddup2(&actions, fds[1], STDOUT_FILENO);
    posix_spawn_file_actions_adddup2(&actions, fds[1], STDERR_FILENO);
    posix_spawn_file_actions_addclose(&actions, fds[0]);
    posix_spawn_file_actions_addclose(&actions, fds[1]);
    posix_spawnattr_t attributes;
    posix_spawnattr_init(&attributes);
    posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETPGROUP);
    posix_spawnattr_setpgroup(&attributes, 0);
    pid_t pid{};
    const auto error = posix_spawnp(&pid, argv[0], &actions, &attributes, argv.data(), env.data());
    posix_spawnattr_destroy(&attributes);
    posix_spawn_file_actions_destroy(&actions);
    close(fds[1]);
    int output = fds[0];
    if (error) {
        close(output);
        throw std::runtime_error("cannot execute " + args.front());
    }
    const auto close_output = [&] {
        if (output >= 0) {
            close(output);
            output = -1;
        }
    };
    fcntl(output, F_SETFL, fcntl(output, F_GETFL) | O_NONBLOCK);
    const auto deadline = monotonic_ns() + static_cast<std::int64_t>(timeout * 1e9);
    std::string result;
    int status = 0;
    bool reaped = false;
    try {
        while (true) {
            char buffer[4096];
            auto count = read(output, buffer, sizeof(buffer));
            if (count > 0) {
                result.append(buffer, static_cast<std::size_t>(count));
                if (result.size() > 1024 * 1024)
                    throw std::runtime_error("command output exceeds bound");
                continue;
            }
            const auto ended = waitpid(pid, &status, WNOHANG);
            if (ended == pid) {
                reaped = true;
                while ((count = read(output, buffer, sizeof(buffer))) > 0) {
                    result.append(buffer, static_cast<std::size_t>(count));
                    if (result.size() > 1024 * 1024)
                        throw std::runtime_error("command output exceeds bound");
                }
                break;
            }
            if (ended < 0 && errno != EINTR)
                throw std::runtime_error("command wait failed");
            if (monotonic_ns() >= deadline)
                throw std::runtime_error("command deadline exceeded: " + args.front());
            pollfd pending{output, POLLIN, 0};
            poll(&pending, 1, 20);
        }
        close_output();
        if (!WIFEXITED(status) || WEXITSTATUS(status) != 0)
            throw std::runtime_error("command failed: " + args.front() + "\n" + result);
        return result;
    } catch (...) {
        close_output();
        if (!reaped) {
            // Terminate and reap the owned command group on failure.
            kill(-pid, SIGKILL);
            while (waitpid(pid, &status, 0) < 0 && errno == EINTR) {
            }
        }
        throw;
    }
}
} // namespace bcamsync::detail
