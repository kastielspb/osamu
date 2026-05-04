#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Integration-test entrypoint
#
# Starts (in order):
#   1. slave_simulator.py   — TCP bridge to Python slave state machines
#   2. klipper.elf (primary) — primary MCU (satisfies Klipper startup)
#   3. klipper.elf (pmu)     — pmu MCU, connects to slave simulator
#   4. klippy.py             — Klipper host
#   5. pytest                — integration test suite
#
# Environment variables (with defaults):
#   SLAVE_COUNT         number of slave nodes         (default: 2)
#   SLAVE_SIM_HOST      slave simulator bind address   (default: 127.0.0.1)
#   SLAVE_SIM_PORT      slave simulator TCP port       (default: 9000)
#   KLIPPY_LOG          klippy log file path           (default: /tmp/klippy.log)
#   KLIPPY_SOCKET       Klipper API socket path        (default: /tmp/klippy_uds)
#   PYTEST_ARGS         extra arguments forwarded to pytest
# ---------------------------------------------------------------------------
set -euo pipefail

SLAVE_COUNT="${SLAVE_COUNT:-2}"
SLAVE_SIM_HOST="${SLAVE_SIM_HOST:-127.0.0.1}"
SLAVE_SIM_PORT="${SLAVE_SIM_PORT:-9000}"
KLIPPY_LOG="${KLIPPY_LOG:-/tmp/klippy.log}"
KLIPPY_SOCKET="${KLIPPY_SOCKET:-/tmp/klippy_uds}"
PYTEST_ARGS="${PYTEST_ARGS:-}"
CONFIG_FILE="/app/tests/printer_test.cfg"
KLIPPER_ELF="/klipper/out/klipper.elf"
KLIPPY_PY="/klipper/klippy/klippy.py"

# ---- helpers ---------------------------------------------------------------

wait_for_file() {
    local path="$1" timeout="${2:-30}" elapsed=0
    while [[ ! -e "$path" ]]; do
        sleep 0.5
        elapsed=$((elapsed + 1))
        if [[ $elapsed -ge $((timeout * 2)) ]]; then
            echo "[entrypoint] timeout waiting for $path" >&2
            return 1
        fi
    done
}

wait_for_tcp() {
    local host="$1" port="$2" timeout="${3:-30}" elapsed=0
    while ! bash -c ">/dev/tcp/$host/$port" 2>/dev/null; do
        sleep 0.5
        elapsed=$((elapsed + 1))
        if [[ $elapsed -ge $((timeout * 2)) ]]; then
            echo "[entrypoint] timeout waiting for $host:$port" >&2
            return 1
        fi
    done
}

# Initialise PIDs so the cleanup trap never references unbound variables.
KLIPPY_PID=0; MCU_PMU_PID=0; MCU_PRIMARY_PID=0; SIM_PID=0

cleanup() {
    echo "[entrypoint] cleaning up…"
    kill "$KLIPPY_PID" "$MCU_PMU_PID" "$MCU_PRIMARY_PID" "$SIM_PID" 2>/dev/null || true
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 1. Start slave simulator
# ---------------------------------------------------------------------------
echo "[entrypoint] starting slave simulator (${SLAVE_COUNT} slaves on ${SLAVE_SIM_HOST}:${SLAVE_SIM_PORT})"
SLAVE_SIM_HOST="$SLAVE_SIM_HOST" \
SLAVE_SIM_PORT="$SLAVE_SIM_PORT" \
python3 /app/tests/slave_simulator.py \
    --slaves "$SLAVE_COUNT" \
    --host   "$SLAVE_SIM_HOST" \
    --port   "$SLAVE_SIM_PORT" \
    &
SIM_PID=$!

wait_for_tcp "$SLAVE_SIM_HOST" "$SLAVE_SIM_PORT" 15
echo "[entrypoint] slave simulator ready"

# ---------------------------------------------------------------------------
# 2. Start primary Linux MCU  (no RS485 traffic — just satisfies [mcu] init)
# ---------------------------------------------------------------------------
echo "[entrypoint] starting primary MCU at /tmp/klipper_primary"
"$KLIPPER_ELF" -I /tmp/klipper_primary &
MCU_PRIMARY_PID=$!

# ---------------------------------------------------------------------------
# 3. Start pmu Linux MCU  (will connect to slave simulator via rs485_linux.c)
# ---------------------------------------------------------------------------
echo "[entrypoint] starting pmu MCU at /tmp/klipper_pmu"
SLAVE_SIM_HOST="$SLAVE_SIM_HOST" \
SLAVE_SIM_PORT="$SLAVE_SIM_PORT" \
"$KLIPPER_ELF" -I /tmp/klipper_pmu &
MCU_PMU_PID=$!

wait_for_file /tmp/klipper_primary 15
wait_for_file /tmp/klipper_pmu    15
echo "[entrypoint] MCU sockets ready"

# ---------------------------------------------------------------------------
# 4. Start klippy
# ---------------------------------------------------------------------------
# Sync bind-mounted klipper_extras into Klipper's extras directory so that
# edits to Python source take effect without rebuilding the image.
echo "[entrypoint] syncing klipper_extras to klippy"
cp /app/klipper_extras/pico_mmu.py /klipper/klippy/extras/pico_mmu.py

echo "[entrypoint] starting klippy"
rm -f "$KLIPPY_SOCKET" "$KLIPPY_LOG"
python3 "$KLIPPY_PY" \
    "$CONFIG_FILE" \
    -l "$KLIPPY_LOG" \
    -a "$KLIPPY_SOCKET" \
    &
KLIPPY_PID=$!

wait_for_file "$KLIPPY_SOCKET" 30
# Poll until klippy is actually ready (the socket may exist before ready).
echo "[entrypoint] waiting for klippy ready state…"
for _ in $(seq 120); do
    # Bail out early if klippy process has already died.
    if ! kill -0 "$KLIPPY_PID" 2>/dev/null; then
        echo "[entrypoint] ERROR: klippy process exited unexpectedly" >&2
        echo "--- klippy log ---" >&2
        tail -50 "$KLIPPY_LOG" >&2
        exit 1
    fi
    STATE=$(python3 -c "
import socket, json
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    s.connect('$KLIPPY_SOCKET')
    s.sendall(b'{\"id\":1,\"method\":\"info\",\"params\":{}}\x03')
    data = b''
    while b'\x03' not in data:
        chunk = s.recv(1024)
        if not chunk: break
        data += chunk
    s.close()
    print(json.loads(data.split(b'\x03')[0])['result']['state'])
except Exception:
    print('error')
" 2>/dev/null)
    if [[ "$STATE" == "ready" ]]; then
        echo "[entrypoint] klippy is ready"
        break
    fi
    sleep 0.5
done

if [[ "$STATE" != "ready" ]]; then
    echo "[entrypoint] ERROR: klippy did not reach ready state" >&2
    echo "--- klippy log ---" >&2
    tail -50 "$KLIPPY_LOG" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 5. Run pytest
# ---------------------------------------------------------------------------
echo "[entrypoint] running integration tests"
# shellcheck disable=SC2086
python3 -m pytest /app/tests/test_integration_klipper.py -v -m integration $PYTEST_ARGS
EXIT_CODE=$?

echo "[entrypoint] tests finished (exit $EXIT_CODE)"
exit "$EXIT_CODE"
