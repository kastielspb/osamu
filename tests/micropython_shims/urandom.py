"""Shim for MicroPython's urandom module."""

import random as _random


def getrandbits(n: int) -> int:
    return _random.getrandbits(n)
