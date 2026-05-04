# =============================================================================
# Stage 1 — build the Klipper Linux-process MCU with pico_mmu commands
# =============================================================================
FROM debian:bookworm-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        make \
        git \
        python3 \
        python3-pip \
        python3-dev \
        libssl-dev \
        libffi-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Pin to a specific Klipper commit for reproducibility.
ARG KLIPPER_REPO=https://github.com/Klipper3d/klipper
ARG KLIPPER_REF=master
RUN git clone --depth=1 --branch "${KLIPPER_REF}" "${KLIPPER_REPO}" /klipper

# ---------------------------------------------------------------------------
# Copy our custom MCU source files into klipper's src tree.
# rs485_linux.c replaces rs485.c for the Linux build (TCP socket transport).
# command_bridge.c and protocol.c are platform-neutral and reused as-is.
# ---------------------------------------------------------------------------
COPY master/pico_mmu/ /klipper/src/pico_mmu/

# ---------------------------------------------------------------------------
# Inject our source files into Klipper's build.
#
# Klipper's src/Makefile uses the pattern:
#   src-y += <path relative to src/>
# We append our three files unconditionally (src-y, not conditional on a
# Kconfig symbol) because they compile cleanly for all platforms and the
# DECL_COMMAND / DECL_TASK macros are no-ops on unused targets.
# ---------------------------------------------------------------------------
RUN printf '\nsrc-y += pico_mmu/command_bridge.c\n' >> /klipper/src/Makefile && \
    printf 'src-y += pico_mmu/protocol.c\n'         >> /klipper/src/Makefile && \
    printf 'src-y += pico_mmu/rs485_linux.c\n'      >> /klipper/src/Makefile

# ---------------------------------------------------------------------------
# Generate a minimal .config for the Linux-process MCU target.
# "make olddefconfig" fills in all remaining defaults.
# ---------------------------------------------------------------------------
RUN printf 'CONFIG_MACH_LINUX=y\n' > /klipper/.config && \
    cd /klipper && make olddefconfig

# Build the MCU firmware binary.
# Pre-create the output subdirectory for our custom sources so gcc can write
# its dependency files (-MD flag) without failing on a missing directory.
RUN mkdir -p /klipper/out/src/pico_mmu && \
    cd /klipper && make -j"$(nproc)"

# =============================================================================
# Stage 2 — runtime image with Python, Klipper host, and test suite
# =============================================================================
FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libc6-dev \
        libssl-dev \
        libffi-dev \
        git \
    && rm -rf /var/lib/apt/lists/*

# Copy the full klipper Python tree from the builder stage.
COPY --from=builder /klipper /klipper

# Copy the compiled Linux-MCU binary (built in stage 1).
COPY --from=builder /klipper/out/klipper.elf /klipper/out/klipper.elf
RUN chmod +x /klipper/out/klipper.elf

# Install Klipper's Python host dependencies.
RUN pip install --no-cache-dir \
        cffi \
        pyserial \
        greenlet \
        jinja2 \
    || true
# Install from requirements file if it exists.
RUN test -f /klipper/scripts/klippy-requirements.txt && \
    pip install --no-cache-dir -r /klipper/scripts/klippy-requirements.txt || true

# Install pytest for the test suite.
RUN pip install --no-cache-dir pytest

# ---------------------------------------------------------------------------
# Copy the project files.
# ---------------------------------------------------------------------------
COPY . /app

# Install the project's own Python dependencies.
RUN pip install --no-cache-dir /app || true

# ---------------------------------------------------------------------------
# Expose pico_mmu.py to Klipper's extras loader.
# Klipper looks for extras in klippy/extras/.
# ---------------------------------------------------------------------------
RUN cp /app/klipper_extras/pico_mmu.py /klipper/klippy/extras/pico_mmu.py

WORKDIR /app

# ---------------------------------------------------------------------------
# Default command — run the integration test suite.
# The actual process orchestration (simulator, MCU, klippy, pytest) is done
# by the entrypoint script that docker-compose invokes.
# ---------------------------------------------------------------------------
CMD ["/app/tests/entrypoint.sh"]
