# Osamu — Agent Instructions

## Common Commands

| Command | Description |
|---------|-------------|
| `make lint` | Format code with ruff and auto-fix lint issues |
| `make test` | Run the full test suite with pytest |

## Development Setup

Create and sync the virtual environment:

```sh
uv venv .venv
uv sync --dev
```

## Project Structure

- `slave/` — MicroPython firmware for the RP2350 slave MCU
- `master/` — C code for the master-side Klipper integration
- `klipper_extras/` — Klipper extra plugin (`pico_mmu.py`)
- `tests/` — Host-side CPython test suite (pytest)
- `tests/micropython_shims/` — Shims that stub out MicroPython-only modules for testing on CPython
- `docs/` — Firmware and hardware specifications
