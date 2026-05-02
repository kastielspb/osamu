"""
RS485 half-duplex driver for RP2350.
Handles UART TX/RX with DE (Direction Enable) pin management.
"""

import uasyncio as asyncio
from machine import UART, Pin
from .protocol import (
    Frame, FrameReader, build_response, parse_frame, ParseError,
    ADDR_UNASSIGNED, PREAMBLE
)
from config import (
    RS485_UART_ID, RS485_TX_PIN, RS485_RX_PIN,
    RS485_DE_PIN, RS485_BAUD
)


class RS485:
    """
    Half-duplex RS485 transceiver driver.

    DE pin HIGH = transmit mode, LOW = receive mode.
    Only transmits in response to master requests (slave mode).
    """

    def __init__(self):
        self._uart = UART(
            RS485_UART_ID,
            baudrate=RS485_BAUD,
            tx=Pin(RS485_TX_PIN),
            rx=Pin(RS485_RX_PIN),
            bits=8, parity=None, stop=1
        )
        self._de = Pin(RS485_DE_PIN, Pin.OUT, value=0)  # Start in RX mode
        self._reader = FrameReader()
        self._rx_buf = bytearray(128)

    def _set_tx_mode(self):
        """Switch transceiver to transmit."""
        self._de.value(1)

    def _set_rx_mode(self):
        """Switch transceiver to receive."""
        self._de.value(0)

    async def send(self, frame_bytes: bytes):
        """
        Send raw frame bytes over RS485.
        Handles DE pin switching with proper timing.
        """
        self._set_tx_mode()
        # Small delay for transceiver to switch (typ. 10-50 ns for MAX485,
        # but MicroPython overhead already covers this)
        self._uart.write(frame_bytes)
        # Wait for transmission to complete
        # At 115200 baud, 1 byte ≈ 87 µs. For a max frame (71 bytes) ≈ 6.2 ms
        await asyncio.sleep_ms(1 + (len(frame_bytes) * 10 * 1000 // RS485_BAUD))
        self._set_rx_mode()

    async def send_response(self, addr: int, cmd: int, seq: int, data: bytes = b''):
        """Build and send a response frame."""
        frame = build_response(addr, cmd, seq, data)
        await self.send(frame)

    def poll_rx(self) -> Frame | None:
        """
        Non-blocking check for received frame.
        Call this frequently from the asyncio loop.

        Returns:
            Parsed Frame or None if no complete frame available.
        """
        # Read any available bytes from UART
        available = self._uart.any()
        if available:
            n = self._uart.readinto(self._rx_buf, min(available, len(self._rx_buf)))
            if n:
                self._reader.feed(self._rx_buf[:n])

        return self._reader.try_parse()

    async def receive(self, timeout_ms: int = 100) -> Frame | None:
        """
        Wait for a complete frame with timeout.

        Args:
            timeout_ms: Maximum time to wait for a frame.

        Returns:
            Parsed Frame or None if timeout.
        """
        deadline = asyncio.ticks_add(asyncio.ticks_ms(), timeout_ms)
        while asyncio.ticks_diff(deadline, asyncio.ticks_ms()) > 0:
            frame = self.poll_rx()
            if frame is not None:
                return frame
            await asyncio.sleep_ms(1)
        return None

    def flush_rx(self):
        """Discard any buffered receive data."""
        while self._uart.any():
            self._uart.read()
        self._reader = FrameReader()
