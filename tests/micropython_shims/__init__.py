"""
MicroPython shims for host-side testing.

Provides minimal replacements for:
  machine.Pin, machine.UART, machine.unique_id
  uasyncio / asyncio
  rp2.StateMachine, rp2.asm_pio, rp2.PIO
  urandom
  time (ticks_ms / ticks_diff)

All hardware-affecting calls are no-ops or return predictable test values.
"""
