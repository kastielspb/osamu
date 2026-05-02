"""
Layer 1 integration test: C <-> Python protocol cross-validation.

Compiles master/pico_mmu/protocol.c into a shared library via ctypes and
verifies that pmu_crc16, pmu_build_frame, and pmu_parse_frame produce
byte-for-byte identical results compared to the Python implementation.

Requires: gcc (skipped automatically if absent)
Run:  bash tests/build_protocol.sh && python3 tests/test_protocol_c.py -v
"""

import ctypes
import os
import struct
import subprocess
import sys

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "slave"))

from bus.protocol import (
    PREAMBLE,
    Addr,
    Cmd,
    CmdDir,
    SlotState,
    Status,
    build_frame,
    build_response,
    crc16_modbus,
    parse_frame,
)

LIB_PATH = os.path.join(REPO_ROOT, "tests", "libprotocol.so")

# ---------------------------------------------------------------------------
# Load shared library (skip if absent or gcc missing)
# ---------------------------------------------------------------------------


def _load_lib():
    if not os.path.exists(LIB_PATH):
        # Try building it automatically
        build_script = os.path.join(REPO_ROOT, "tests", "build_protocol.sh")
        try:
            subprocess.run(["bash", build_script], check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None
    try:
        lib = ctypes.CDLL(LIB_PATH)
    except OSError:
        return None

    # pmu_crc16(data, len) -> uint16_t
    lib.pmu_crc16.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
    lib.pmu_crc16.restype = ctypes.c_uint16

    # pmu_build_frame(buf, addr, cmd, seq, data, data_len) -> size_t
    lib.pmu_build_frame.argtypes = [
        ctypes.c_char_p,  # buf (output, caller-allocated)
        ctypes.c_uint8,  # addr
        ctypes.c_uint8,  # cmd
        ctypes.c_uint8,  # seq
        ctypes.c_char_p,  # data (can be NULL)
        ctypes.c_uint8,  # data_len
    ]
    lib.pmu_build_frame.restype = ctypes.c_size_t

    # struct pmu_frame { addr, cmd, seq, data_len, data[64] }
    class PmuFrame(ctypes.Structure):
        _fields_ = [
            ("addr", ctypes.c_uint8),
            ("cmd", ctypes.c_uint8),
            ("seq", ctypes.c_uint8),
            ("data_len", ctypes.c_uint8),
            ("data", ctypes.c_uint8 * 64),
        ]

    lib.PmuFrame = PmuFrame

    # pmu_parse_frame(buf, buf_len, frame) -> int
    lib.pmu_parse_frame.argtypes = [
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.POINTER(PmuFrame),
    ]
    lib.pmu_parse_frame.restype = ctypes.c_int

    return lib


LIB = _load_lib()
GCC_AVAILABLE = LIB is not None

skip_no_gcc = pytest.mark.skipif(not GCC_AVAILABLE, reason="gcc / libprotocol.so not available")


# ---------------------------------------------------------------------------
# Helper: build frame in C
# ---------------------------------------------------------------------------


def _c_build_frame(addr, cmd, seq, data=b""):
    buf = ctypes.create_string_buffer(128)
    c_data = ctypes.c_char_p(data) if data else None
    length = LIB.pmu_build_frame(buf, addr, cmd, seq, c_data, len(data))
    assert length > 0, "pmu_build_frame returned 0"
    return bytes(buf[:length])


def _c_parse_frame(frame_bytes):
    frame = LIB.PmuFrame()
    consumed = LIB.pmu_parse_frame(frame_bytes, len(frame_bytes), ctypes.byref(frame))
    return consumed, frame


# ---------------------------------------------------------------------------
# CRC tests
# ---------------------------------------------------------------------------


@skip_no_gcc
def test_crc_empty():
    assert LIB.pmu_crc16(b"", 0) == crc16_modbus(b"")


@skip_no_gcc
def test_crc_known_vector():
    data = b"123456789"
    assert LIB.pmu_crc16(data, len(data)) == 0x4B37
    assert crc16_modbus(data) == 0x4B37


@skip_no_gcc
def test_crc_matches_python_on_all_commands():
    """CRC must be identical for every command type frame header."""
    headers = [bytes([PREAMBLE, 0x01, int(c), 0x00, 0x00]) for c in Cmd]
    for h in headers:
        c_crc = LIB.pmu_crc16(h, len(h))
        py_crc = crc16_modbus(h)
        assert c_crc == py_crc, (
            f"CRC mismatch for header {h.hex()}: C={c_crc:#06x} Py={py_crc:#06x}"
        )


# ---------------------------------------------------------------------------
# Frame building: Python → parse in C
# ---------------------------------------------------------------------------


@skip_no_gcc
def test_ping_python_to_c():
    py_frame = build_frame(0x01, Cmd.PING, 0x00)
    consumed, parsed = _c_parse_frame(py_frame)
    assert consumed == len(py_frame)
    assert parsed.addr == 0x01
    assert (parsed.cmd & 0x7F) == int(Cmd.PING)
    assert parsed.data_len == 0


@skip_no_gcc
def test_feed_python_to_c():
    data = struct.pack("<BH", 2, 800)  # slot=2, speed=800 Hz
    py_frame = build_frame(0x03, Cmd.FEED, 0x0A, data)
    consumed, parsed = _c_parse_frame(py_frame)
    assert consumed == len(py_frame)
    assert (parsed.cmd & 0x7F) == int(Cmd.FEED)
    assert parsed.data_len == 3
    assert bytes(parsed.data[:3]) == data


@skip_no_gcc
def test_set_filament_color_python_to_c():
    data = bytes([0, 255, 128, 0])  # slot=0, R=255, G=128, B=0
    py_frame = build_frame(0x01, Cmd.SET_FILAMENT_COLOR, 0x01, data)
    consumed, parsed = _c_parse_frame(py_frame)
    assert consumed == len(py_frame)
    assert (parsed.cmd & 0x7F) == int(Cmd.SET_FILAMENT_COLOR)
    assert parsed.data_len == 4
    assert bytes(parsed.data[:4]) == data


@skip_no_gcc
def test_response_python_to_c():
    data = bytes([int(Status.OK)])
    py_frame = build_response(0x02, Cmd.PING, 0x05, data)
    consumed, parsed = _c_parse_frame(py_frame)
    assert consumed == len(py_frame)
    assert parsed.cmd & 0x80, "Response bit should be set"
    assert (parsed.cmd & 0x7F) == int(Cmd.PING)


@skip_no_gcc
def test_max_payload_python_to_c():
    data = bytes(range(64))
    py_frame = build_frame(0x01, Cmd.GET_STATUS, 0x00, data)
    consumed, parsed = _c_parse_frame(py_frame)
    assert consumed == len(py_frame)
    assert parsed.data_len == 64
    assert bytes(parsed.data[:64]) == data


# ---------------------------------------------------------------------------
# Frame building: C → parse in Python
# ---------------------------------------------------------------------------


@skip_no_gcc
def test_ping_c_to_python():
    c_frame = _c_build_frame(0x01, int(Cmd.PING), 0x00)
    py_parsed = parse_frame(c_frame)
    assert py_parsed.addr == 0x01
    assert py_parsed.cmd == Cmd.PING
    assert py_parsed.data == b""
    assert not py_parsed.is_response


@skip_no_gcc
def test_discover_broadcast_c_to_python():
    uid = b"\x01\x23\x45\x67\x89\xab\xcd\xef"
    c_frame = _c_build_frame(
        int(Addr.UNASSIGNED), int(Cmd.DISCOVER) | int(CmdDir.RESPONSE), 0x01, uid
    )
    py_parsed = parse_frame(c_frame)
    assert py_parsed.addr == Addr.UNASSIGNED
    assert py_parsed.cmd == Cmd.DISCOVER
    assert py_parsed.is_response
    assert py_parsed.data == uid


@skip_no_gcc
def test_get_status_response_c_to_python():
    payload = bytes(
        [
            int(SlotState.LOADED),
            int(SlotState.EMPTY),
            int(SlotState.FEEDING),
            int(SlotState.ASSIST),
            0x03,  # sensors bitmask
            0x00,  # errors
        ]
    )
    c_frame = _c_build_frame(0x01, int(Cmd.GET_STATUS) | 0x80, 0x07, payload)
    py_parsed = parse_frame(c_frame)
    assert py_parsed.is_response
    assert py_parsed.data == payload


@skip_no_gcc
def test_bad_crc_detected_by_python():
    c_frame = bytearray(_c_build_frame(0x01, int(Cmd.PING), 0x00))
    c_frame[-1] ^= 0xFF  # corrupt CRC
    from bus.protocol import ParseError

    with pytest.raises(ParseError, match="CRC"):
        parse_frame(bytes(c_frame))


@skip_no_gcc
def test_bad_crc_detected_by_c():
    py_frame = bytearray(build_frame(0x01, Cmd.PING, 0x00))
    py_frame[-1] ^= 0xFF  # corrupt CRC
    frame = LIB.PmuFrame()
    result = LIB.pmu_parse_frame(bytes(py_frame), len(py_frame), ctypes.byref(frame))
    assert result < 0, "C parser should return negative on bad CRC"


# ---------------------------------------------------------------------------
# Round-trip fuzz: 100 random frames Python→C→Python
# ---------------------------------------------------------------------------


@skip_no_gcc
def test_roundtrip_fuzz():
    import random

    rng = random.Random(42)
    for _ in range(100):
        addr = rng.randint(1, 254)
        cmd = rng.choice(list(Cmd))
        seq = rng.randint(0, 255)
        dlen = rng.randint(0, 64)
        data = bytes(rng.randint(0, 255) for _ in range(dlen))

        py_frame = build_frame(addr, cmd, seq, data)
        consumed, c_parsed = _c_parse_frame(py_frame)
        assert consumed == len(py_frame)
        assert (c_parsed.cmd & 0x7F) == int(cmd)
        assert c_parsed.addr == addr
        assert c_parsed.data_len == dlen
        assert bytes(c_parsed.data[:dlen]) == data
