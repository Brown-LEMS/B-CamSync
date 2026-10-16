#!/usr/bin/env bash
# Confirm Rust accesses and rebuilt std synchronization use the C++ TSan runtime.
set -euo pipefail
source_root=$1
smoke_root=$2
: "${BCAMSYNC_RUST_TSAN_TOOLCHAIN:?Use the project image or select the pinned nightly with rust-src}"
smoke_target=$(rustc "+$BCAMSYNC_RUST_TSAN_TOOLCHAIN" -vV | sed -n 's/^host: //p')
mkdir -p "$smoke_root"
printf 'Building the Rust ThreadSanitizer instrumentation probe with %s\n' "$BCAMSYNC_RUST_TSAN_TOOLCHAIN"
CARGO_TARGET_DIR="$smoke_root" \
    RUSTFLAGS="-Zsanitizer=thread -Zexternal-clangrt -Clinker=$CXX -Clink-arg=-fsanitize=thread -Clink-arg=-lstdc++" \
    cargo "+$BCAMSYNC_RUST_TSAN_TOOLCHAIN" build --locked --release -Zbuild-std \
        --target "$smoke_target" --manifest-path "$source_root/tools/tsan-smoke/Cargo.toml"
smoke_binary="$smoke_root/$smoke_target/release/bcamsync-tsan-smoke"
printf 'Checking the Rust mutex-protected path\n'
if ! TSAN_OPTIONS=halt_on_error=1:exitcode=66 "$smoke_binary" mutex > "$smoke_root/mutex.log" 2>&1; then
    cat "$smoke_root/mutex.log" >&2
    exit 1
fi
printf 'Checking that an intentional Rust race is detected\n'
if TSAN_OPTIONS=halt_on_error=1:exitcode=66 "$smoke_binary" race > "$smoke_root/race.log" 2>&1; then
    printf 'ThreadSanitizer failed to detect the intentional Rust race.\n' >&2
    exit 1
else
    smoke_status=$?
fi
if [[ $smoke_status != 66 ]] || ! grep -q 'WARNING: ThreadSanitizer: data race' "$smoke_root/race.log"; then
    cat "$smoke_root/race.log" >&2
    printf 'The Rust race probe failed without the expected ThreadSanitizer report.\n' >&2
    exit 1
fi
printf 'Rust race detection and standard-library synchronization checks passed; reports: %s\n' "$smoke_root"
