ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake ninja-build pkg-config nlohmann-json3-dev libsystemd-dev curl \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/bcamsync/venv
ENV VIRTUAL_ENV=/opt/bcamsync/venv
ENV PATH="/opt/bcamsync/venv/bin:${PATH}"
RUN python -m pip install --no-cache-dir \
    scikit-build-core==0.11.6 pybind11==3.1.0

ARG RUST_VERSION=1.99.0
ENV CARGO_HOME=/opt/rust/cargo
ENV RUSTUP_HOME=/opt/rust/rustup
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o /tmp/rustup-init.sh \
    && sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain "${RUST_VERSION}" \
    && rm /tmp/rustup-init.sh
ENV PATH="/opt/rust/cargo/bin:${PATH}"
WORKDIR /src
CMD ["bash", "tools/build_wheel.sh", "/wheels"]
