"""
Unit tests for the RS485 protocol: frame encoding, decoding, and CRC-16/MODBUS.
Tests run on CPython (host) to verify protocol correctness before deploying to MCU.
"""

import sys
import os
import struct

# Add slave/bus to path for importing protocol module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'slave'))

from bus.protocol import (
    crc16_modbus, build_frame, build_response, parse_frame, ParseError,
    Frame, FrameReader,
    PREAMBLE, ADDR_BROADCAST, ADDR_UNASSIGNED,
    CMD_PING, CMD_DISCOVER, CMD_ASSIGN_ADDR, CMD_GET_STATUS,
    CMD_FEED, CMD_RETRACT, CMD_SET_ASSIST, CMD_STOP, CMD_STOP_ALL,
    CMD_DIR_RESPONSE,
    STATUS_OK, STATUS_BUSY, STATUS_ERROR_SLOT_EMPTY,
    SLOT_EMPTY, SLOT_LOADED, SLOT_FEEDING, SLOT_ASSIST,
)


# --- CRC-16/MODBUS Tests ---

def test_crc16_empty():
    assert crc16_modbus(b'') == 0xFFFF

def test_crc16_known_vectors():
    # Standard Modbus CRC test vector: "123456789" → 0x4B37
    assert crc16_modbus(b'123456789') == 0x4B37

def test_crc16_single_byte():
    crc = crc16_modbus(b'\x01')
    assert crc == 0x807E

def test_crc16_deterministic():
    data = b'\xAA\x01\x04\x00\x00'
    crc1 = crc16_modbus(data)
    crc2 = crc16_modbus(data)
    assert crc1 == crc2


# --- Frame Building Tests ---

def test_build_frame_ping():
    frame = build_frame(0x01, CMD_PING, 0x00)
    assert frame[0] == PREAMBLE
    assert frame[1] == 0x01  # addr
    assert frame[2] == CMD_PING  # cmd
    assert frame[3] == 0x00  # seq
    assert frame[4] == 0x00  # len (no data)
    assert len(frame) == 7  # header(5) + crc(2)

def test_build_frame_with_data():
    data = b'\x02\x03\x20'  # slot=2, speed=0x0320 (800 Hz)
    frame = build_frame(0x03, CMD_FEED, 0x0A, data)
    assert frame[0] == PREAMBLE
    assert frame[1] == 0x03  # addr
    assert frame[2] == CMD_FEED  # cmd
    assert frame[3] == 0x0A  # seq
    assert frame[4] == 3     # len
    assert frame[5:8] == data
    assert len(frame) == 10  # header(5) + data(3) + crc(2)

def test_build_response():
    frame = build_response(0x01, CMD_PING, 0x05, b'\x00\x01')
    assert frame[2] == (CMD_PING | CMD_DIR_RESPONSE)  # Response bit set

def test_build_frame_max_data():
    data = bytes(range(64))
    frame = build_frame(0x01, CMD_GET_STATUS, 0x00, data)
    assert frame[4] == 64
    assert len(frame) == 5 + 64 + 2

def test_build_frame_data_too_long():
    data = bytes(65)
    try:
        build_frame(0x01, CMD_PING, 0x00, data)
        assert False, "Should raise ValueError"
    except ValueError:
        pass

def test_build_frame_broadcast():
    frame = build_frame(ADDR_BROADCAST, CMD_STOP_ALL, 0x01)
    assert frame[1] == 0x00


# --- Frame Parsing Tests ---

def test_parse_frame_roundtrip():
    """Build a frame and parse it back — should be identical."""
    original_data = b'\x01\x02\x03\x04'
    frame_bytes = build_frame(0x05, CMD_GET_STATUS, 0x0F, original_data)
    parsed = parse_frame(frame_bytes)
    assert parsed.addr == 0x05
    assert parsed.cmd == CMD_GET_STATUS
    assert parsed.seq == 0x0F
    assert parsed.data == original_data
    assert not parsed.is_response

def test_parse_response_frame():
    frame_bytes = build_response(0x02, CMD_PING, 0x03, b'\x00\x01')
    parsed = parse_frame(frame_bytes)
    assert parsed.addr == 0x02
    assert parsed.cmd == CMD_PING  # Direction bit stripped
    assert parsed.is_response
    assert parsed.data == b'\x00\x01'

def test_parse_frame_too_short():
    try:
        parse_frame(b'\xAA\x01\x02')
        assert False, "Should raise ParseError"
    except ParseError:
        pass

def test_parse_frame_bad_preamble():
    try:
        parse_frame(b'\xBB\x01\x01\x00\x00\x00\x00')
        assert False, "Should raise ParseError"
    except ParseError:
        pass

def test_parse_frame_bad_crc():
    frame = build_frame(0x01, CMD_PING, 0x00)
    # Corrupt last byte
    corrupted = frame[:-1] + bytes([frame[-1] ^ 0xFF])
    try:
        parse_frame(corrupted)
        assert False, "Should raise ParseError"
    except ParseError as e:
        assert "CRC" in str(e)

def test_parse_frame_zero_length():
    frame = build_frame(0x01, CMD_PING, 0x00)
    parsed = parse_frame(frame)
    assert parsed.data == b''


# --- FrameReader (streaming parser) Tests ---

def test_frame_reader_single_frame():
    reader = FrameReader()
    frame_bytes = build_frame(0x01, CMD_PING, 0x00)
    reader.feed(frame_bytes)
    frame = reader.try_parse()
    assert frame is not None
    assert frame.cmd == CMD_PING
    assert reader.try_parse() is None  # No more frames

def test_frame_reader_multiple_frames():
    reader = FrameReader()
    f1 = build_frame(0x01, CMD_PING, 0x01)
    f2 = build_frame(0x02, CMD_GET_STATUS, 0x02)
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
    frame_bytes = build_frame(0x01, CMD_FEED, 0x05, b'\x02\x00\x03')

    # Feed byte by byte
    for i, b in enumerate(frame_bytes):
        reader.feed(bytes([b]))
        result = reader.try_parse()
        if i < len(frame_bytes) - 1:
            assert result is None  # Not complete yet
        else:
            assert result is not None
            assert result.cmd == CMD_FEED
            assert result.data == b'\x02\x00\x03'

def test_frame_reader_garbage_prefix():
    reader = FrameReader()
    garbage = b'\x00\x01\x02\x03\xFF\xFE'
    frame_bytes = build_frame(0x01, CMD_PING, 0x00)
    reader.feed(garbage + frame_bytes)

    frame = reader.try_parse()
    assert frame is not None
    assert frame.cmd == CMD_PING

def test_frame_reader_corrupted_frame_skipped():
    reader = FrameReader()
    # Build a frame but corrupt CRC
    bad_frame = build_frame(0x01, CMD_PING, 0x00)
    bad_frame = bad_frame[:-1] + bytes([bad_frame[-1] ^ 0xFF])
    good_frame = build_frame(0x02, CMD_GET_STATUS, 0x01)

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
    unique_id = b'\x01\x23\x45\x67\x89\xAB\xCD\xEF'

    # Master sends DISCOVER broadcast
    req = build_frame(ADDR_UNASSIGNED, CMD_DISCOVER, 0x01)
    parsed_req = parse_frame(req)
    assert parsed_req.addr == ADDR_UNASSIGNED
    assert parsed_req.cmd == CMD_DISCOVER

    # Slave responds with unique_id
    resp = build_response(ADDR_UNASSIGNED, CMD_DISCOVER, 0x01, unique_id)
    parsed_resp = parse_frame(resp)
    assert parsed_resp.is_response
    assert parsed_resp.data == unique_id

def test_assign_addr_roundtrip():
    """Simulate ASSIGN_ADDR command."""
    unique_id = b'\x01\x23\x45\x67\x89\xAB\xCD\xEF'
    new_addr = 0x01

    # Master sends ASSIGN_ADDR
    data = unique_id + bytes([new_addr])
    req = build_frame(ADDR_UNASSIGNED, CMD_ASSIGN_ADDR, 0x02, data)
    parsed = parse_frame(req)
    assert parsed.data[:8] == unique_id
    assert parsed.data[8] == new_addr

def test_get_status_response():
    """Simulate GET_STATUS response parsing."""
    # 4 slot states + 1 sensor byte + 1 error byte
    slot_states = bytes([SLOT_LOADED, SLOT_FEEDING, SLOT_EMPTY, SLOT_ASSIST])
    sensors = bytes([0x03])  # Slots 0,1 have filament
    errors = bytes([0x00])

    resp = build_response(0x01, CMD_GET_STATUS, 0x0A, slot_states + sensors + errors)
    parsed = parse_frame(resp)
    assert parsed.data[0] == SLOT_LOADED
    assert parsed.data[1] == SLOT_FEEDING
    assert parsed.data[2] == SLOT_EMPTY
    assert parsed.data[3] == SLOT_ASSIST
    assert parsed.data[4] == 0x03  # sensors bitmask

def test_feed_command():
    """Test FEED command with slot and speed."""
    slot = 2
    speed_hz = 800
    data = struct.pack('<BH', slot, speed_hz)
    frame = build_frame(0x03, CMD_FEED, 0x05, data)
    parsed = parse_frame(frame)
    assert parsed.data[0] == 2
    assert struct.unpack('<H', parsed.data[1:3])[0] == 800


# --- CRC cross-validation with C implementation ---

def test_crc16_cross_validate():
    """
    Test vectors that should match the C implementation (pmu_crc16).
    Use these to verify both implementations produce identical results.
    """
    vectors = [
        (b'\xAA\x01\x01\x00\x00', None),  # PING to addr 1
        (b'\xAA\xFF\x02\x01\x00', None),  # DISCOVER broadcast
        (b'\xAA\x03\x05\x0A\x03\x02\x03\x20', None),  # FEED slot 2, speed 800
    ]

    # Just ensure deterministic output for documentation
    results = []
    for data, _ in vectors:
        crc = crc16_modbus(data)
        results.append((data.hex(), f"0x{crc:04X}"))

    # Print for manual verification against C output
    print("\nCRC-16 Test Vectors (for C cross-validation):")
    for data_hex, crc_hex in results:
        print(f"  data={data_hex} → crc={crc_hex}")

    # Verify they're all non-zero and 16-bit
    for _, crc_hex in results:
        crc = int(crc_hex, 16)
        assert 0 < crc < 0xFFFF


# --- Run all tests ---

def run_tests():
    test_functions = [obj for name, obj in globals().items()
                      if name.startswith('test_') and callable(obj)]

    passed = 0
    failed = 0

    for test in test_functions:
        try:
            test()
            passed += 1
            print(f"  PASS: {test.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL: {test.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR: {test.__name__}: {type(e).__name__}: {e}")

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")
    return failed == 0


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
