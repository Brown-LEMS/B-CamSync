FROM debian:12-slim

ARG LIBCAMERA_VERSION=0.5.2
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc-12 g++-12 cmake ninja-build pkg-config git ca-certificates \
    clang-16 clang-tidy-16 llvm-16 libclang-rt-16-dev \
    python3 python3-dev python3-venv \
    nlohmann-json3-dev libyaml-dev libudev-dev libsystemd-dev network-manager chrony \
    && rm -rf /var/lib/apt/lists/*

# The Linux environment has its own interpreter and packages.
RUN python3 -m venv /opt/bcamsync/venv
ENV VIRTUAL_ENV=/opt/bcamsync/venv
ENV PATH="/opt/bcamsync/venv/bin:${PATH}"
COPY tools/container-requirements.txt /opt/bcamsync/container-requirements.txt
RUN python -m pip install --no-cache-dir -r /opt/bcamsync/container-requirements.txt

# Compile the pinned official API for backend build checks. Device installations use their vendor SDK.
RUN git clone --depth 1 --branch "v${LIBCAMERA_VERSION}" \
    https://github.com/libcamera-org/libcamera.git /opt/libcamera-source \
    && CC=gcc-12 CXX=g++-12 meson setup /opt/libcamera-build /opt/libcamera-source \
       --prefix=/opt/libcamera --libdir=lib --buildtype=release \
       -Dpipelines=uvcvideo -Dipas=[] -Dcam=disabled -Dqcam=disabled \
       -Ddocumentation=disabled -Dgstreamer=disabled -Dlc-compliance=disabled \
       -Dpycamera=disabled -Dtracing=disabled -Dv4l2=disabled -Dtest=false \
    && meson compile -C /opt/libcamera-build -j 2 \
    && meson install -C /opt/libcamera-build \
    && rm -rf /opt/libcamera-source /opt/libcamera-build

ARG RUST_VERSION=1.99.0
ARG RUST_TSAN_TOOLCHAIN=nightly-2026-10-15
ENV CARGO_HOME=/opt/rust/cargo
ENV RUSTUP_HOME=/opt/rust/rustup
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o /tmp/rustup-init.sh \
    && sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain "${RUST_VERSION}" \
    && rm /tmp/rustup-init.sh
ENV PATH="/opt/rust/cargo/bin:${PATH}"
# Thread checks rebuild Rust and std together; ordinary builds retain the stable default.
RUN rustup toolchain install "${RUST_TSAN_TOOLCHAIN}" --profile minimal --component rust-src
ENV BCAMSYNC_RUST_TSAN_TOOLCHAIN=${RUST_TSAN_TOOLCHAIN}

ENV PKG_CONFIG_PATH=/opt/libcamera/lib/pkgconfig
ENV LD_LIBRARY_PATH=/opt/libcamera/lib
ENV ASAN_SYMBOLIZER_PATH=/usr/bin/llvm-symbolizer-16
ENV BCAMSYNC_BUILD_ROOT=/work/build
WORKDIR /src
CMD ["bash"]
