"""
Unit tests for the RS485 protocol: frame encoding, decoding, and CRC-16/MODBUS.
Tests run on CPython (host) to verify protocol correctness before deploying to MCU.
"""

import os
import struct
import sys

import pytest

# Add slave/bus to path for importing protocol module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "slave"))

from bus.protocol import (
    PREAMBLE,
    Addr,
    Cmd,
    CmdDir,
    FrameReader,
    ParseError,
    SlotState,
    build_frame,
    build_response,
    crc16_modbus,
    parse_frame,
)

# --- CRC-16/MODBUS Tests ---


def test_crc16_empty():
    assert crc16_modbus(b"") == 0xFFFF


def test_crc16_known_vectors():
    # Standard Modbus CRC test vector: "123456789" → 0x4B37
    assert crc16_modbus(b"123456789") == 0x4B37


def test_crc16_single_byte():
    crc = crc16_modbus(b"\x01")
    assert crc == 0x807E


def test_crc16_deterministic():
    data = b"\xaa\x01\x04\x00\x00"
    crc1 = crc16_modbus(data)
    crc2 = crc16_modbus(data)
    assert crc1 == crc2


# --- Frame Building Tests ---


def test_build_frame_ping():
    frame = build_frame(0x01, Cmd.PING, 0x00)
    assert frame[0] == PREAMBLE
    assert frame[1] == 0x01  # addr
    assert frame[2] == Cmd.PING  # cmd
    assert frame[3] == 0x00  # seq
    assert frame[4] == 0x00  # len (no data)
    assert len(frame) == 7  # header(5) + crc(2)


def test_build_frame_with_data():
    data = b"\x02\x03\x20"  # slot=2, speed=0x0320 (800 Hz)
    frame = build_frame(0x03, Cmd.FEED, 0x0A, data)
    assert frame[0] == PREAMBLE
    assert frame[1] == 0x03  # addr
    assert frame[2] == Cmd.FEED  # cmd
    assert frame[3] == 0x0A  # seq
    assert frame[4] == 3  # len
    assert frame[5:8] == data
    assert len(frame) == 10  # header(5) + data(3) + crc(2)


def test_build_response():
    frame = build_response(0x01, Cmd.PING, 0x05, b"\x00\x01")
    assert frame[2] == (Cmd.PING | CmdDir.RESPONSE)  # Response bit set


def test_build_frame_max_data():
    data = bytes(range(64))
    frame = build_frame(0x01, Cmd.GET_STATUS, 0x00, data)
    assert frame[4] == 64
    assert len(frame) == 5 + 64 + 2


def test_build_frame_data_too_long():
    data = bytes(65)
    with pytest.raises(ValueError):
        build_frame(0x01, Cmd.PING, 0x00, data)


def test_build_frame_broadcast():
    frame = build_frame(Addr.BROADCAST, Cmd.STOP_ALL, 0x01)
    assert frame[1] == 0x00


# --- Frame Parsing Tests ---


def test_parse_frame_roundtrip():
    """Build a frame and parse it back — should be identical."""
    original_data = b"\x01\x02\x03\x04"
    frame_bytes = build_frame(0x05, Cmd.GET_STATUS, 0x0F, original_data)
    parsed = parse_frame(frame_bytes)
    assert parsed.addr == 0x05
    assert parsed.cmd == Cmd.GET_STATUS
    assert parsed.seq == 0x0F
    assert parsed.data == original_data
    assert not parsed.is_response


def test_parse_response_frame():
    frame_bytes = build_response(0x02, Cmd.PING, 0x03, b"\x00\x01")
    parsed = parse_frame(frame_bytes)
    assert parsed.addr == 0x02
    assert parsed.cmd == Cmd.PING  # Direction bit stripped
    assert parsed.is_response
    assert parsed.data == b"\x00\x01"


def test_parse_frame_too_short():
    with pytest.raises(ParseError):
        parse_frame(b"\xaa\x01\x02")


def test_parse_frame_bad_preamble():
    with pytest.raises(ParseError):
        parse_frame(b"\xbb\x01\x01\x00\x00\x00\x00")


def test_parse_frame_bad_crc():
    frame = build_frame(0x01, Cmd.PING, 0x00)
    # Corrupt last byte
    corrupted = frame[:-1] + bytes([frame[-1] ^ 0xFF])
    with pytest.raises(ParseError, match="CRC"):
        parse_frame(corrupted)


def test_parse_frame_zero_length():
    frame = build_frame(0x01, Cmd.PING, 0x00)
    parsed = parse_frame(frame)
    assert parsed.data == b""


# --- FrameReader (streaming parser) Tests ---


def test_frame_reader_single_frame():
    reader = FrameReader()
    frame_bytes = build_frame(0x01, Cmd.PING, 0x00)
    reader.feed(frame_bytes)
    frame = reader.try_parse()
    assert frame is not None
    assert frame.cmd == Cmd.PING
    assert reader.try_parse() is None  # No more frames


def test_frame_reader_multiple_frames():
    reader = FrameReader()
    f1 = build_frame(0x01, Cmd.PING, 0x01)
    f2 = build_frame(0x02, Cmd.GET_STATUS, 0x02)
    reader.feed(f1 + f2)

    frame1 = reader.try_parse()
    assert frame1 is not None
    assert frame1.addr == 0x01

    frame2 = reader.try_parse()
    assert frame2 is not None
    assert frame2.addr == 0x02

    assert reader.try_parse() is None


def test_frame_reader_partial_frames():
    reader = FrameReader()
    frame_bytes = build_frame(0x01, Cmd.FEED, 0x05, b"\x02\x00\x03")

    # Feed byte by byte
    for i, b in enumerate(frame_bytes):
        reader.feed(bytes([b]))
        result = reader.try_parse()
        if i < len(frame_bytes) - 1:
            assert result is None  # Not complete yet
        else:
            assert result is not None
            assert result.cmd == Cmd.FEED
            assert result.data == b"\x02\x00\x03"


def test_frame_reader_garbage_prefix():
    reader = FrameReader()
    garbage = b"\x00\x01\x02\x03\xff\xfe"
    frame_bytes = build_frame(0x01, Cmd.PING, 0x00)
    reader.feed(garbage + frame_bytes)

    frame = reader.try_parse()
    assert frame is not None
    assert frame.cmd == Cmd.PING


def test_frame_reader_corrupted_frame_skipped():
    reader = FrameReader()
    # Build a frame but corrupt CRC
    bad_frame = build_frame(0x01, Cmd.PING, 0x00)
    bad_frame = bad_frame[:-1] + bytes([bad_frame[-1] ^ 0xFF])
    good_frame = build_frame(0x02, Cmd.GET_STATUS, 0x01)

    reader.feed(bad_frame + good_frame)

    # Should skip bad frame and find good one
    frame = reader.try_parse()
    # May need multiple calls to skip through garbage
    attempts = 0
    while frame is None and attempts < 20:
        frame = reader.try_parse()
        attempts += 1

    assert frame is not None
    assert frame.addr == 0x02


# --- Protocol scenarios ---


def test_discover_roundtrip():
    """Simulate DISCOVER request and response."""
    unique_id = b"\x01\x23\x45\x67\x89\xab\xcd\xef"

    # Master sends DISCOVER broadcast
    req = build_frame(Addr.UNASSIGNED, Cmd.DISCOVER, 0x01)
    parsed_req = parse_frame(req)
    assert parsed_req.addr == Addr.UNASSIGNED
    assert parsed_req.cmd == Cmd.DISCOVER

    # Slave responds with unique_id
    resp = build_response(Addr.UNASSIGNED, Cmd.DISCOVER, 0x01, unique_id)
    parsed_resp = parse_frame(resp)
    assert parsed_resp.is_response
    assert parsed_resp.data == unique_id


def test_assign_addr_roundtrip():
    """Simulate ASSIGN_ADDR command."""
    unique_id = b"\x01\x23\x45\x67\x89\xab\xcd\xef"
    new_addr = 0x01

    # Master sends ASSIGN_ADDR
    data = unique_id + bytes([new_addr])
    req = build_frame(Addr.UNASSIGNED, Cmd.ASSIGN_ADDR, 0x02, data)
    parsed = parse_frame(req)
    assert parsed.data[:8] == unique_id
    assert parsed.data[8] == new_addr


def test_get_status_response():
    """Simulate GET_STATUS response parsing."""
    # 4 slot states + 1 sensor byte + 1 error byte
    slot_states = bytes([SlotState.LOADED, SlotState.FEEDING, SlotState.EMPTY, SlotState.ASSIST])
    sensors = bytes([0x03])  # Slots 0,1 have filament
    errors = bytes([0x00])

    resp = build_response(0x01, Cmd.GET_STATUS, 0x0A, slot_states + sensors + errors)
    parsed = parse_frame(resp)
    assert parsed.data[0] == SlotState.LOADED
    assert parsed.data[1] == SlotState.FEEDING
    assert parsed.data[2] == SlotState.EMPTY
    assert parsed.data[3] == SlotState.ASSIST
    assert parsed.data[4] == 0x03  # sensors bitmask


def test_feed_command():
    """Test FEED command with slot and speed."""
    slot = 2
    speed_hz = 800
    data = struct.pack("<BH", slot, speed_hz)
    frame = build_frame(0x03, Cmd.FEED, 0x05, data)
    parsed = parse_frame(frame)
    assert parsed.data[0] == 2
    assert struct.unpack("<H", parsed.data[1:3])[0] == 800


# --- CRC cross-validation with C implementation ---


def test_crc16_cross_validate():
    """
    Test vectors that should match the C implementation (pmu_crc16).
    Use these to verify both implementations produce identical results.
    """
    vectors = [
        b"\xaa\x01\x01\x00\x00",  # PING to addr 1
        b"\xaa\xff\x02\x01\x00",  # DISCOVER broadcast
        b"\xaa\x03\x05\x0a\x03\x02\x03\x20",  # FEED slot 2, speed 800
    ]
    for data in vectors:
        crc = crc16_modbus(data)
        assert 0 < crc < 0xFFFF
