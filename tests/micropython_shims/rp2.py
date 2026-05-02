"""
Shim for MicroPython's rp2 module.

Provides: StateMachine, asm_pio decorator, PIO constants.
All hardware operations are no-ops.
"""


class _PIOConstants:
    OUT_LOW = 0
    OUT_HIGH = 1
    SHIFT_LEFT = 0
    SHIFT_RIGHT = 1
    IN_LOW = 0
    IN_HIGH = 1


PIO = _PIOConstants()


def asm_pio(**kwargs):
    """Decorator: marks a function as a PIO program. No-op on CPython."""

    def decorator(fn):
        return fn

    return decorator


class StateMachine:
    """No-op PIO state machine stub."""

    def __init__(
        self,
        sm_id,
        program=None,
        freq=None,
        set_base=None,
        sideset_base=None,
        in_base=None,
        out_base=None,
        jmp_pin=None,
        in_shiftdir=None,
        out_shiftdir=None,
        push_thresh=None,
        pull_thresh=None,
    ):
        self._id = sm_id
        self._active = False

    def active(self, value=None):
        if value is not None:
            self._active = bool(value)
        return self._active

    def put(self, value, shift=0):
        pass

    def get(self, buf=None, shift=0):
        return 0

    def restart(self):
        pass
