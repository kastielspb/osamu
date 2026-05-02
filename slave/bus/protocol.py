"""
RS485 Binary Protocol: frame encoding/decoding and CRC-16/MODBUS.
Shared logic between Master (test/extras) and Slave (MicroPython).
"""

import struct

# --- Constants ---
PREAMBLE = 0xAA
MAX_DATA_LEN = 64
HEADER_SIZE = 5  # PREAMBLE + ADDR + CMD + SEQ + LEN
CRC_SIZE = 2
MIN_FRAME_SIZE = HEADER_SIZE + CRC_SIZE  # 7 bytes (no data)

# Addresses
ADDR_BROADCAST = 0x00
ADDR_UNASSIGNED = 0xFF

# CMD bit 7: direction
CMD_DIR_REQUEST = 0x00
CMD_DIR_RESPONSE = 0x80

# Command codes (Master → Slave)
CMD_PING = 0x01
CMD_DISCOVER = 0x02
CMD_ASSIGN_ADDR = 0x03
CMD_GET_STATUS = 0x04
CMD_FEED = 0x05
CMD_RETRACT = 0x06
CMD_SET_ASSIST = 0x07
CMD_STOP = 0x08
CMD_STOP_ALL = 0x09
CMD_SET_LED = 0x0A
CMD_GET_CONFIG = 0x0B
CMD_SET_CURRENT = 0x0C
CMD_HOME_SLOT = 0x0D

# Status codes
STATUS_OK = 0x00
STATUS_BUSY = 0x01
STATUS_ERROR_SLOT_EMPTY = 0x02
STATUS_ERROR_JAM = 0x03
STATUS_ERROR_TIMEOUT = 0x04
STATUS_ERROR_INVALID_SLOT = 0x05
STATUS_UNKNOWN_CMD = 0xFF

# Slot states
SLOT_EMPTY = 0x00
SLOT_LOADED = 0x01
SLOT_FEEDING = 0x02
SLOT_RETRACTING = 0x03
SLOT_ASSIST = 0x04
SLOT_ERROR = 0x05

# LED modes
LED_MODE_OFF = 0x00
LED_MODE_SOLID = 0x01
LED_MODE_BREATHE = 0x02
LED_MODE_BLINK = 0x03


# --- CRC-16/MODBUS ---
def crc16_modbus(data: bytes) -> int:
    """Compute CRC-16/MODBUS over a byte sequence."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


# --- Frame Building ---
def build_frame(addr: int, cmd: int, seq: int, data: bytes = b'') -> bytes:
    """
    Build a complete RS485 frame.

    Args:
        addr: Device address (0x00=broadcast, 0x01-0xFE=slave, 0xFF=unassigned)
        cmd: Command code (bit7 = direction: 0=request, 1=response)
        seq: Sequence number (0-255)
        data: Payload bytes (0-64)

    Returns:
        Complete frame bytes including preamble and CRC16.
    """
    if len(data) > MAX_DATA_LEN:
        raise ValueError(f"Data too long: {len(data)} > {MAX_DATA_LEN}")

    header = struct.pack('BBBBB', PREAMBLE, addr, cmd, seq, len(data))
    frame_no_crc = header + data
    crc = crc16_modbus(frame_no_crc)
    return frame_no_crc + struct.pack('<H', crc)


def build_response(addr: int, cmd: int, seq: int, data: bytes = b'') -> bytes:
    """Build a response frame (sets bit7 on cmd)."""
    return build_frame(addr, cmd | CMD_DIR_RESPONSE, seq, data)


# --- Frame Parsing ---
class ParseError(Exception):
    pass


class Frame:
    """Parsed RS485 frame."""
    __slots__ = ('addr', 'cmd', 'seq', 'data', 'is_response')

    def __init__(self, addr: int, cmd: int, seq: int, data: bytes):
        self.addr = addr
        self.cmd = cmd & 0x7F  # strip direction bit
        self.is_response = bool(cmd & CMD_DIR_RESPONSE)
        self.seq = seq
        self.data = data

    def __repr__(self):
        direction = 'RSP' if self.is_response else 'REQ'
        return (f"Frame({direction} addr=0x{self.addr:02X} "
                f"cmd=0x{self.cmd:02X} seq={self.seq} "
                f"data={self.data.hex()})")


def parse_frame(buf: bytes) -> Frame:
    """
    Parse a complete RS485 frame from buffer.

    Args:
        buf: Raw bytes (must be a complete frame)

    Returns:
        Parsed Frame object

    Raises:
        ParseError: If frame is malformed or CRC check fails
    """
    if len(buf) < MIN_FRAME_SIZE:
        raise ParseError(f"Frame too short: {len(buf)} < {MIN_FRAME_SIZE}")

    if buf[0] != PREAMBLE:
        raise ParseError(f"Invalid preamble: 0x{buf[0]:02X}")

    addr, cmd, seq, length = buf[1], buf[2], buf[3], buf[4]

    if length > MAX_DATA_LEN:
        raise ParseError(f"Data length too large: {length}")

    expected_size = HEADER_SIZE + length + CRC_SIZE
    if len(buf) < expected_size:
        raise ParseError(f"Frame incomplete: have {len(buf)}, need {expected_size}")

    data = buf[HEADER_SIZE:HEADER_SIZE + length]
    crc_received = struct.unpack_from('<H', buf, HEADER_SIZE + length)[0]
    crc_computed = crc16_modbus(buf[:HEADER_SIZE + length])

    if crc_received != crc_computed:
        raise ParseError(
            f"CRC mismatch: received 0x{crc_received:04X}, "
            f"computed 0x{crc_computed:04X}")

    return Frame(addr, cmd, seq, data)


class FrameReader:
    """
    Incremental frame parser for stream-based RS485 reception.
    Buffers incoming bytes and yields complete frames.
    """

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes):
        """Add received bytes to buffer."""
        self._buf.extend(data)

    def try_parse(self):
        """
        Attempt to extract one complete frame from buffer.

        Returns:
            Frame if a valid frame was found, None otherwise.
            Discards bytes before a valid preamble.
        """
        # Find preamble
        while self._buf and self._buf[0] != PREAMBLE:
            self._buf.pop(0)

        if len(self._buf) < MIN_FRAME_SIZE:
            return None

        # Check if we have enough bytes
        length = self._buf[4]
        if length > MAX_DATA_LEN:
            # Invalid length, discard this preamble and search for next
            self._buf.pop(0)
            return None

        expected_size = HEADER_SIZE + length + CRC_SIZE
        if len(self._buf) < expected_size:
            return None

        # Try to parse
        frame_bytes = bytes(self._buf[:expected_size])
        try:
            frame = parse_frame(frame_bytes)
            del self._buf[:expected_size]
            return frame
        except ParseError:
            # Bad frame, skip this preamble
            self._buf.pop(0)
            return None
