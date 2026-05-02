"""
Shim for the MicroPython 'machine' module.

Provides: Pin, UART, unique_id()

The UART shim is backed by a pair of bytearrays (tx_buffer / rx_buffer)
that tests can inspect and populate directly, making it easy to inject
raw RS485 frames without real hardware.
"""

import threading

# Fixed unique_id returned by unique_id() — 8 bytes, RP2350-style
_UNIQUE_ID = b"\xde\xad\xbe\xef\xca\xfe\xba\xbe"


def unique_id() -> bytes:
    return _UNIQUE_ID


# ---------------------------------------------------------------------------
# Pin
# ---------------------------------------------------------------------------


class Pin:
    IN = 0
    OUT = 1
    PULL_UP = 2
    PULL_DOWN = 3

    def __init__(self, id, mode=IN, pull=None, value=None):
        self._id = id
        self._mode = mode
        # Physical pull-up inputs are HIGH by default when nothing is connected.
        if value is not None:
            self._value = int(value)
        elif mode == Pin.IN and pull == Pin.PULL_UP:
            self._value = 1  # default HIGH for pulled-up inputs
        else:
            self._value = 0

    def value(self, v=None):
        if v is None:
            return self._value
        self._value = int(v)

    def __call__(self, v=None):
        return self.value(v)


# ---------------------------------------------------------------------------
# UART
# ---------------------------------------------------------------------------


class UART:
    """
    Minimal UART shim backed by in-memory buffers.

    Tests write bytes into `rx_inject` to simulate incoming data.
    Data written via `write()` accumulates in `tx_captured`.
    """

    def __init__(
        self,
        id,
        *,
        baudrate=115200,
        tx=None,
        rx=None,
        bits=8,
        parity=None,
        stop=1,
        timeout=0,
        timeout_char=0,
    ):
        self._id = id
        self._lock = threading.Lock()
        # Bytes that poll_rx / any() / read() will return
        self.rx_inject = bytearray()
        # Bytes written by the slave via write()
        self.tx_captured = bytearray()

    def any(self) -> int:
        with self._lock:
            return len(self.rx_inject)

    def read(self, nbytes=None) -> bytes:
        with self._lock:
            if nbytes is None:
                data = bytes(self.rx_inject)
                self.rx_inject.clear()
            else:
                data = bytes(self.rx_inject[:nbytes])
                del self.rx_inject[:nbytes]
        return data

    def readinto(self, buf, nbytes=None) -> int:
        n = nbytes if nbytes is not None else len(buf)
        data = self.read(n)
        buf[: len(data)] = data
        return len(data)

    def write(self, data) -> int:
        with self._lock:
            self.tx_captured.extend(data)
        return len(data)

    def inject(self, data: bytes):
        """Test helper: push bytes into the RX buffer."""
        with self._lock:
            self.rx_inject.extend(data)

    def drain_tx(self) -> bytes:
        """Test helper: consume and return all bytes written by the slave."""
        with self._lock:
            data = bytes(self.tx_captured)
            self.tx_captured.clear()
        return data
