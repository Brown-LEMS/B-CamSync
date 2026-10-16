#pragma once
#include "bcamsync/phase.hpp"
#include <nlohmann/json.hpp>

namespace bcamsync::detail {
PhasePolicy phase_policy_from_json(const nlohmann::json& value);
nlohmann::json phase_policy_json(const PhasePolicy& policy);
} // namespace bcamsync::detail
