#!/usr/bin/env bash
# Verify the installed CMake package and Python module inside the build image.
set -euo pipefail

if [[ $# -ne 4 ]]; then
  echo "Usage: $0 SOURCE_ROOT BUILD_DIRECTORY INSTALL_PREFIX PYTHON_EXECUTABLE" >&2
  exit 2
fi

source_root=$(cd "$1" && pwd)
build_directory=$(cd "$2" && pwd)
mkdir -p "$3"
install_prefix=$(cd "$3" && pwd)
python_executable=$4
consumer_build="$build_directory/installed-consumer"

echo "[1/4] Install B-CamSync into $install_prefix"
cmake --install "$build_directory" --prefix "$install_prefix"
echo "[2/4] Build an independent CMake consumer"
cmake -S "$source_root/examples/cmake_consumer" -B "$consumer_build" \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$install_prefix"
cmake --build "$consumer_build" --parallel 2
echo "[3/4] Run installed C++ acquisition and ownership checks"
"$consumer_build/consumer" "$source_root/examples/configs/fake_one_node.json"
echo "[4/4] Import the CMake-installed Python package"
PYTHONPATH="$install_prefix" "$python_executable" - "$install_prefix" \
  "$source_root/examples/configs/fake_one_node.json" <<'PY'
from pathlib import Path
import sys

import bcamsync
from bcamsync import Config, _native

prefix = Path(sys.argv[1]).resolve()
for module in (bcamsync, _native):
    module_path = Path(module.__file__).resolve()
    if not module_path.is_relative_to(prefix):
        raise RuntimeError(f"Module was not loaded from installation prefix: {module_path}")
    print(f"Installed module: {module_path}")
config = Config.from_file(Path(sys.argv[2]))
if config.backend != "fake" or len(config.units) != 2:
    raise RuntimeError("Installed Python package did not load the fake topology")
print("Installed Python package loaded the public two-board configuration")
PY
