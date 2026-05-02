"""
Shim for MicroPython's uasyncio module.

Provides just enough to allow slave/state_machine.py and slave/bus/rs485.py
to import without errors. The real asyncio loop used in tests is CPython's
standard asyncio (imported as 'asyncio' at the test level).

For dispatch tests (Layer 2), we call _handle_frame() directly and never
actually run the event loop, so these stubs only need to satisfy imports
and the handful of places where asyncio is called at module scope.
"""

import asyncio as _asyncio


def sleep_ms(ms):
    """Coroutine: yield for ms milliseconds."""
    return _asyncio.sleep(ms / 1000.0)


def sleep(s):
    return _asyncio.sleep(s)


def create_task(coro):
    """Schedule a coroutine. No-op in stub context; real loop handles it."""
    try:
        loop = _asyncio.get_event_loop()
        return loop.create_task(coro)
    except RuntimeError:
        # No running loop — just consume the coroutine object silently
        coro.close()
        return None


def run(coro):
    return _asyncio.run(coro)


# Allow 'import uasyncio as asyncio' usage
get_event_loop = _asyncio.get_event_loop
