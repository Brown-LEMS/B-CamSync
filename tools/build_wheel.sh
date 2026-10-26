#!/usr/bin/env bash
# Build and exercise a core wheel in tools/wheels.Dockerfile's virtual environment.
set -euo pipefail
source_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
destination=${1:?Specify the wheel output directory}
python="${VIRTUAL_ENV:?Activate the wheel build environment}/bin/python"
export CMAKE_BUILD_PARALLEL_LEVEL=${BCAMSYNC_JOBS:-2}
export PYTHONDONTWRITEBYTECODE=1

printf 'Building the synchronization core wheel with %s\n' "$("$python" --version)"
"$python" -m pip wheel --verbose --no-build-isolation --no-deps "$source_root" --wheel-dir "$destination" \
    --config-setting=cmake.define.CMAKE_INSTALL_LIBDIR=lib

printf 'Installing the wheel into a clean virtual environment\n'
test_root=$(mktemp -d)
trap 'rm -rf "$test_root"' EXIT
"$python" -m venv "$test_root/venv"
wheel_python="$test_root/venv/bin/python"
"$wheel_python" -m pip install --no-index --no-deps "$destination"/bcamsync-*.whl
cd "$test_root"
unset PYTHONPATH
printf 'Checking installed interfaces and SDK-free adapter behavior\n'
"$wheel_python" -m unittest discover -s "$source_root/tests" -p test_component_boundary.py -v
"$wheel_python" -m unittest discover -s "$source_root/tests" -p test_adapters.py -v
"$wheel_python" -m unittest discover -s "$source_root/tools" -p 'test_*wheel*.py' -v
printf 'Wheel build and installation checks completed\n'
