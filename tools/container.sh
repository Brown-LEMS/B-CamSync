#!/usr/bin/env bash
# Launch the same Linux development image used by GitHub Actions.
set -euo pipefail
source_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
image=${BCAMSYNC_IMAGE:-bcamsync-dev:bookworm}
platform=${BCAMSYNC_PLATFORM:-linux/arm64}
if [[ ${1:-} == image ]]; then
    exec docker build --platform "$platform" -t "$image" "$source_root"
fi
mkdir -p "$source_root/build-linux"
if [[ $# == 0 ]]; then
    exec docker run --rm -it --platform "$platform" \
        --mount "type=bind,source=$source_root,target=/src,readonly" \
        --mount "type=bind,source=$source_root/build-linux,target=/work/build" \
        -e BCAMSYNC_TOOLCHAIN="${BCAMSYNC_TOOLCHAIN:-gcc}" \
        -w /src "$image" bash
fi
exec docker run --rm --platform "$platform" \
    --mount "type=bind,source=$source_root,target=/src,readonly" \
    --mount "type=bind,source=$source_root/build-linux,target=/work/build" \
    -e BCAMSYNC_TOOLCHAIN="${BCAMSYNC_TOOLCHAIN:-gcc}" \
    -e BCAMSYNC_JOBS="${BCAMSYNC_JOBS:-2}" \
    -e BCAMSYNC_FUZZ_SECONDS \
    -w /src "$image" bash tools/build.sh "$@"
