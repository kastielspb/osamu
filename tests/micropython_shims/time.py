"""
Shim that augments CPython's time module with MicroPython's ticks_* API.

Import order matters: slave code does 'import time' then calls time.ticks_ms().
This module must be importable as 'time' in the shim context.
We re-export everything from the real time module plus add the missing functions.
"""

import time as _real_time
from time import *  # re-export sleep, monotonic, etc.


def ticks_ms() -> int:
    """Return current time in milliseconds (monotonic)."""
    return int(_real_time.monotonic() * 1000)


def ticks_diff(newer: int, older: int) -> int:
    """Signed difference between two ticks_ms() values."""
    return newer - older


def ticks_add(ticks: int, delta: int) -> int:
    return ticks + delta
