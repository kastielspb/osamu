#!/bin/bash
# Compile master/pico_mmu/protocol.c into a shared library for ctypes testing.
# Run from repo root or tests/ directory.

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO_ROOT/master/pico_mmu/protocol.c"
OUT="$REPO_ROOT/tests/libprotocol.so"

if ! command -v gcc &>/dev/null; then
    echo "ERROR: gcc not found. Install build-essential to run Layer 1 tests." >&2
    exit 1
fi

gcc -shared -fPIC -O2 \
    -I"$REPO_ROOT/master/pico_mmu" \
    "$SRC" \
    -o "$OUT"

echo "Built: $OUT"
