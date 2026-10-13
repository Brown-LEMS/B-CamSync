#pragma once
#include <string>
#include <vector>
namespace bcamsync::detail {
std::string run_program(const std::vector<std::string>& args, double timeout);
}
