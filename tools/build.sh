#!/usr/bin/env bash
# One Linux build entry point for the development container, CI, and prepared nodes.
set -euo pipefail
source_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
action=${1:-build}
toolchain=${BCAMSYNC_TOOLCHAIN:-gcc}
build_root=${BCAMSYNC_BUILD_ROOT:-$source_root/build-linux}
jobs=${BCAMSYNC_JOBS:-2}
case "$action" in
    build|software|release|sdk|package) ;;
    address|thread|fuzz|analyze) toolchain=clang ;;
    *) printf 'Usage: %s {build|software|release|sdk|package|address|thread|fuzz|analyze}\n' "$0" >&2; exit 2 ;;
esac
[[ $(uname -s) == Linux ]] || { printf 'Run this entry point in Linux.\n' >&2; exit 2; }
[[ $jobs =~ ^[1-9][0-9]*$ ]] || { printf 'BCAMSYNC_JOBS must be positive.\n' >&2; exit 2; }
case "$toolchain" in
    gcc) compiler=g++-12 ;;
    clang) compiler=clang++-16 ;;
    *) printf 'BCAMSYNC_TOOLCHAIN must be gcc or clang.\n' >&2; exit 2 ;;
esac
# The mounted Mac environment is not executable in Linux; the image provides its own venv.
if [[ -x "$source_root/.venv/bin/python" ]]; then
    python="$source_root/.venv/bin/python"
elif [[ -n ${VIRTUAL_ENV:-} && -x "$VIRTUAL_ENV/bin/python" ]]; then
    python="$VIRTUAL_ENV/bin/python"
elif [[ -n ${CONDA_PREFIX:-} && -x "$CONDA_PREFIX/bin/python" ]]; then
    python="$CONDA_PREFIX/bin/python"
else
    printf 'Prepare a project venv or activate a Linux virtual environment before building.\n' >&2
    exit 2
fi
mkdir -p "$build_root/logs" "$build_root/packages"
exec > >(tee "$build_root/logs/$toolchain-$action.log") 2>&1
export PYTHONDONTWRITEBYTECODE=1
export CMAKE_BUILD_PARALLEL_LEVEL="$jobs"
export CXX="$compiler"
printf 'Building %s with %s on %s; outputs: %s\n' "$action" "$compiler" "$(uname -m)" "$build_root"
build_dir="$build_root/$toolchain-$action"
if [[ $action == build ]]; then build_dir="$build_root/$toolchain-software"; fi
progress() { printf '\n[%s] %s\n' "$action" "$*"; }
configure() {
    cmake -S "$source_root" -B "$build_dir" -G Ninja \
        -DCMAKE_CXX_COMPILER="$compiler" -DCMAKE_BUILD_TYPE="$1" \
        -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DCMAKE_INSTALL_LIBDIR=lib \
        -DCMAKE_INSTALL_PREFIX="$build_dir/install" \
        -DBCAMSYNC_WITH_LIBCAMERA="$2" -DBCAMSYNC_BUILD_TESTS="$3" \
        -DBCAMSYNC_BUILD_PYTHON=ON -DBCAMSYNC_BUILD_FUZZER=OFF -DBCAMSYNC_SANITIZER= \
        -DPython_EXECUTABLE="$python" \
        -Dpybind11_DIR="$("$python" -m pybind11 --cmakedir)" "${@:4}"
}
build() { cmake --build "$build_dir" --parallel "$jobs"; }
tests() { ctest --test-dir "$build_dir" --output-on-failure --timeout 180; }
case "$action" in
    build|software|release)
        progress 'Configure native library, Python extension, and software checks'
        if [[ $action == release ]]; then configure Release ON ON; else configure Debug OFF ON; fi
        progress 'Compile'; build
        if [[ $action != build ]]; then
            progress 'Run native and Python acquisition checks'; tests
            progress 'Verify CMake and Python installation from an independent consumer'
            bash "$source_root/tools/check_install.sh" "$source_root" "$build_dir" "$build_dir/install" "$python"
        fi
        ;;
    sdk)
        progress 'Compile the real backend and Python interface against the available SDK'
        pkg-config --modversion libcamera
        configure Debug ON OFF
        build
        ;;
    package)
        progress 'Build Release C++ installation with the real backend'
        configure Release ON OFF
        build
        cmake --install "$build_dir"
        destination="$build_root/packages/$toolchain-$(uname -m)"
        mkdir -p "$destination"
        tar -czf "$destination/bcamsync-cpp.tar.gz" -C "$build_dir/install" include lib bin
        progress 'Build source distribution and Python wheel'
        export CC=$([[ $toolchain == gcc ]] && printf gcc-12 || printf clang-16)
        "$python" -m build --no-isolation --outdir "$destination" \
            --config-setting=cmake.define.BCAMSYNC_WITH_LIBCAMERA=ON \
            --config-setting=cmake.define.CMAKE_INSTALL_LIBDIR=lib \
            --config-setting=cmake.define.CMAKE_CXX_COMPILER="$compiler"
        progress 'Install the wheel in an independent Linux virtual environment'
        "$python" -m venv "$build_dir/wheel-env"
        "$build_dir/wheel-env/bin/python" -m pip install --force-reinstall --no-deps "$destination"/*.whl
        # NumPy is a public dependency installed from the image's pinned requirements.
        "$build_dir/wheel-env/bin/python" -m pip install \
            "$(grep '^numpy==' "$source_root/tools/container-requirements.txt")"
        progress 'Build and install the independent host package without node dependencies'
        host_destination="$build_root/packages/host"
        mkdir -p "$host_destination"
        host_source="$build_dir/host-source"
        mkdir -p "$host_source"
        cp -R "$source_root/host/." "$host_source/"
        "$python" -m build --no-isolation "$host_source" --outdir "$host_destination"
        "$python" -m venv "$build_dir/host-env"
        "$build_dir/host-env/bin/python" -m pip install --force-reinstall --no-deps "$host_destination"/*.whl
        env -u PYTHONPATH "$build_dir/host-env/bin/python" - <<'PY'
import importlib.util
import bcamsync_host
assert importlib.util.find_spec("bcamsync") is None
assert importlib.util.find_spec("numpy") is None
print(f"Independent host installation: {bcamsync_host.__file__}")
PY
        progress 'Exercise installed-wheel acquisition and retained arrays'
        cd "$source_root"
        env -u PYTHONPATH "$build_dir/wheel-env/bin/python" -m unittest discover -s tests -p test_fake_python.py -v
        env -u PYTHONPATH "$build_dir/wheel-env/bin/python" -m unittest discover -s tests -p test_sync_python.py -v
        ;;
    address|thread)
        progress 'Configure separate Clang instrumentation'
        configure Debug OFF ON -DBCAMSYNC_SANITIZER="$action"
        build
        if [[ $action == address ]]; then
            progress 'Check native ownership with ASan, UBSan, and LeakSanitizer'
            export ASAN_OPTIONS=detect_leaks=1:detect_stack_use_after_return=1:halt_on_error=1
            export UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1
            ctest --test-dir "$build_dir" --output-on-failure --timeout 180 \
                -E '^(python_ownership|fake_python|external_python|host_client|device_workflow|python_management|python_preview)$'
            progress 'Check Python arrays with the matching Clang ASan runtime'
            runtime=$("$compiler" -print-file-name="libclang_rt.asan-$(uname -m).so")
            [[ -f $runtime ]] || { printf 'Missing Clang ASan runtime: %s\n' "$runtime" >&2; exit 1; }
            ASAN_OPTIONS=detect_leaks=0:detect_stack_use_after_return=1:halt_on_error=1 \
                LD_PRELOAD="$runtime" ctest --test-dir "$build_dir" --output-on-failure --timeout 180 \
                -R '^(python_ownership|fake_python|external_python|python_management|python_preview)$'
        else
            progress 'Check native acquisition and shutdown under TSan'
            TSAN_OPTIONS=halt_on_error=1 ctest --test-dir "$build_dir" --output-on-failure --timeout 180 \
                -E '^(python_ownership|fake_python|external_python|host_client|device_workflow|python_management|python_preview)$'
        fi
        ;;
    fuzz)
        seconds=${BCAMSYNC_FUZZ_SECONDS:-60}
        [[ $seconds =~ ^[1-9][0-9]*$ ]] || { printf 'BCAMSYNC_FUZZ_SECONDS must be positive.\n' >&2; exit 2; }
        progress 'Build the LLVM timing and message fuzz target'
        configure Debug OFF OFF -DBCAMSYNC_BUILD_PYTHON=OFF -DBCAMSYNC_BUILD_FUZZER=ON
        cmake --build "$build_dir" --target fuzz_timing --parallel "$jobs"
        mkdir -p "$build_dir/corpus" "$build_dir/findings"
        printf '\000' > "$build_dir/corpus/timing"
        printf '\001%s' '{"state":"tracking","ready":true,"fault":"","units":[]}' > "$build_dir/corpus/status"
        progress "Fuzz for $seconds seconds"
        ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 \
            "$build_dir/fuzz_timing" "$build_dir/corpus" -max_total_time="$seconds" \
            -max_len=16384 -timeout=5 -rss_limit_mb=2048 -print_final_stats=1 \
            -artifact_prefix="$build_dir/findings/"
        ;;
    analyze)
        progress 'Compile the real backend for Clang static analysis'
        configure Debug ON OFF
        build
        progress 'Analyze owned buffers, resources, and error paths'
        clang-tidy-16 -p "$build_dir" \
            --checks='-*,clang-analyzer-*,bugprone-use-after-move,bugprone-dangling-handle' \
            --header-filter='.*bcamsync/.*' --warnings-as-errors='*' \
            "$source_root"/src/{types,sync_types,phase,wire,process,platform,sync_runtime,sync,session,fake,arducam}.cpp
        ;;
esac
progress 'Completed'
