"""
Layer 2 integration test: Slave state machine dispatch on CPython.

Installs MicroPython shims, instantiates SlaveController with an assigned
address, injects raw RS485 frames via the fake UART, and drives
_handle_frame() directly (no real asyncio loop required).

Verifies: protocol dispatch, state transitions, and filament-color storage.

Run: python3 tests/test_slave_dispatch.py -v
"""

import asyncio
import os
import struct
import sys

# ---------------------------------------------------------------------------
# Path setup: shims first, then slave directory
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SHIMS_DIR = os.path.join(REPO_ROOT, "tests", "micropython_shims")
SLAVE_DIR = os.path.join(REPO_ROOT, "slave")

# Shims override MicroPython-only modules when importing slave code on CPython
sys.path.insert(0, SLAVE_DIR)
sys.path.insert(0, SHIMS_DIR)

# Patch ticks_ms / ticks_diff into the already-loaded 'time' module so that
# slave code doing 'import time; time.ticks_ms()' gets our implementations.
import time as _real_time

if not hasattr(_real_time, "ticks_ms"):
    _real_time.ticks_ms = lambda: int(_real_time.monotonic() * 1000)
    _real_time.ticks_diff = lambda newer, older: newer - older
    _real_time.ticks_add = lambda t, d: t + d
if not hasattr(_real_time, "sleep_us"):
    _real_time.sleep_us = lambda us: None
if not hasattr(_real_time, "sleep_ms"):
    _real_time.sleep_ms = lambda ms: None

# Register MicroPython-only modules in sys.modules before slave imports them
import importlib
import importlib.util


def _load_shim(name):
    path = os.path.join(SHIMS_DIR, name.replace(".", os.sep) + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


for _shim in ("machine", "uasyncio", "rp2", "urandom"):
    if _shim not in sys.modules:
        _load_shim(_shim)

# ---------------------------------------------------------------------------
# Import slave modules (now that shims are on the path)
# ---------------------------------------------------------------------------
from bus.protocol import (
    Addr,
    Cmd,
    LedMode,
    SlotState,
    Status,
    build_frame,
    build_response,
    parse_frame,
)
from state_machine import SlaveController

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SLAVE_ADDR = 0x01


def _make_controller():
    """Create a SlaveController pre-assigned to SLAVE_ADDR."""
    ctrl = SlaveController()
    ctrl._addr = SLAVE_ADDR
    # Suppress writing addr.cfg in tests
    ctrl._save_address = lambda a: None
    return ctrl


def _send_frame(ctrl, cmd, data=b"", seq=0x01, addr=SLAVE_ADDR):
    """Build a request frame and drive _handle_frame; return response bytes or None."""
    frame_bytes = build_frame(addr, cmd, seq, data)
    parsed = parse_frame(frame_bytes)

    # Drive the coroutine synchronously
    response_bytes = None
    original_send = ctrl._rs485.send_response

    async def _capture(a, c, s, d=b""):
        nonlocal response_bytes
        response_bytes = build_response(a, c, s, d)

    ctrl._rs485.send_response = _capture

    asyncio.run(ctrl._handle_frame(parsed))

    ctrl._rs485.send_response = original_send
    return response_bytes


def _parse_response(resp_bytes):
    """Parse a captured response frame and return the Frame object."""
    assert resp_bytes is not None, "No response was sent"
    return parse_frame(resp_bytes)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ping():
    ctrl = _make_controller()
    resp = _send_frame(ctrl, Cmd.PING)
    frame = _parse_response(resp)
    assert frame.is_response
    assert frame.cmd == Cmd.PING
    assert len(frame.data) == 2, "PING response must be 2 bytes (FW version)"


def test_get_config():
    ctrl = _make_controller()
    resp = _send_frame(ctrl, Cmd.GET_CONFIG)
    frame = _parse_response(resp)
    assert frame.is_response
    assert frame.cmd == Cmd.GET_CONFIG
    # unique_id(8) + addr(1) + fw_ver(2) + num_slots(1) = 12 bytes
    assert len(frame.data) == 12
    assert frame.data[8] == SLAVE_ADDR
    assert frame.data[11] == 4  # 4 slots


def test_get_status_initial():
    ctrl = _make_controller()
    resp = _send_frame(ctrl, Cmd.GET_STATUS)
    frame = _parse_response(resp)
    assert frame.is_response
    assert frame.cmd == Cmd.GET_STATUS
    assert len(frame.data) == 6
    # All slots start EMPTY
    for i in range(4):
        assert frame.data[i] == SlotState.EMPTY


def test_stop_all():
    ctrl = _make_controller()
    resp = _send_frame(ctrl, Cmd.STOP_ALL)
    frame = _parse_response(resp)
    assert frame.is_response
    assert frame.data[0] == Status.OK


def test_ping_invalid_address_ignored():
    """A frame addressed to someone else must be silently dropped."""
    ctrl = _make_controller()
    resp = _send_frame(ctrl, Cmd.PING, addr=0x05)  # not our address
    assert resp is None


def test_broadcast_accepted():
    """Broadcast address frames must be processed."""
    ctrl = _make_controller()
    # STOP_ALL sent to broadcast — no individual response expected for broadcasts
    frame_bytes = build_frame(Addr.BROADCAST, Cmd.STOP_ALL, 0x01)
    parsed = parse_frame(frame_bytes)
    sent = []

    async def _capture(a, c, s, d=b""):
        sent.append(True)

    ctrl._rs485.send_response = _capture
    asyncio.run(ctrl._handle_frame(parsed))
    # Broadcasts must not generate responses
    assert len(sent) == 0


def test_feed_slot_empty_returns_error():
    """Feeding an empty slot (no sensor trigger) returns ERROR_SLOT_EMPTY."""
    ctrl = _make_controller()
    data = struct.pack("<BH", 0, 800)  # slot=0, speed=800
    resp = _send_frame(ctrl, Cmd.FEED, data)
    frame = _parse_response(resp)
    assert frame.data[0] == Status.ERROR_SLOT_EMPTY


def test_feed_invalid_slot():
    ctrl = _make_controller()
    data = struct.pack("<BH", 9, 800)  # slot=9, invalid
    resp = _send_frame(ctrl, Cmd.FEED, data)
    frame = _parse_response(resp)
    assert frame.data[0] == Status.ERROR_INVALID_SLOT


def test_feed_with_filament_present():
    """If sensor is triggered, feed should succeed."""
    ctrl = _make_controller()
    # Simulate filament in slot 0
    ctrl._sensors.sensors[0]._state = True
    ctrl._slots[0].update_from_sensor()  # transition to LOADED

    assert ctrl._slots[0].state == SlotState.LOADED

    data = struct.pack("<BH", 0, 800)
    resp = _send_frame(ctrl, Cmd.FEED, data)
    frame = _parse_response(resp)
    assert frame.data[0] == Status.OK
    assert ctrl._slots[0].state == SlotState.FEEDING


def test_stop_after_feed():
    """STOP on a feeding slot returns OK and transitions state."""
    ctrl = _make_controller()
    ctrl._sensors.sensors[0]._state = True
    ctrl._slots[0].update_from_sensor()

    # Start feeding
    feed_data = struct.pack("<BH", 0, 800)
    _send_frame(ctrl, Cmd.FEED, feed_data)
    assert ctrl._slots[0].state == SlotState.FEEDING

    # Stop
    resp = _send_frame(ctrl, Cmd.STOP, bytes([0]))
    frame = _parse_response(resp)
    assert frame.data[0] == Status.OK
    # With sensor still triggered, slot returns to LOADED
    assert ctrl._slots[0].state == SlotState.LOADED


def test_set_current():
    ctrl = _make_controller()
    data = struct.pack("<BHH", 1, 600, 100)  # slot=1, run=600mA, hold=100mA
    resp = _send_frame(ctrl, Cmd.SET_CURRENT, data)
    frame = _parse_response(resp)
    assert frame.data[0] == Status.OK


def test_set_filament_idle_slot():
    """SET_FILAMENT stores filament info; LED stays off if slot is EMPTY."""
    ctrl = _make_controller()
    data = bytes([2, 255, 128, 0]) + b"PETG"  # slot=2, orange, PETG
    resp = _send_frame(ctrl, Cmd.SET_FILAMENT, data)
    frame = _parse_response(resp)
    assert frame.data[0] == Status.OK
    assert ctrl._filament_info[2] == (255, 128, 0, b"PETG")
    # Slot is EMPTY, so LED should still be off
    assert ctrl._leds._modes[2] == LedMode.OFF


def test_set_filament_loaded_slot():
    """SET_FILAMENT on a LOADED slot updates the LED immediately."""
    ctrl = _make_controller()
    # Force slot 1 into LOADED state
    ctrl._sensors.sensors[1]._state = True
    ctrl._slots[1].update_from_sensor()
    assert ctrl._slots[1].state == SlotState.LOADED

    data = bytes([1, 0, 0, 255]) + b"PLA\x00"  # slot=1, blue, PLA
    resp = _send_frame(ctrl, Cmd.SET_FILAMENT, data)
    frame = _parse_response(resp)
    assert frame.data[0] == Status.OK
    assert ctrl._filament_info[1] == (0, 0, 255, b"PLA")
    # LED should immediately reflect the new color
    assert ctrl._leds._modes[1] == LedMode.SOLID
    assert ctrl._leds._colors[1][:3] == (0, 0, 255)


def test_set_filament_invalid_slot():
    ctrl = _make_controller()
    data = bytes([7, 255, 0, 0]) + b"PLA\x00"  # slot=7, invalid
    resp = _send_frame(ctrl, Cmd.SET_FILAMENT, data)
    frame = _parse_response(resp)
    assert frame.data[0] == Status.ERROR_INVALID_SLOT


def test_set_filament_short_payload():
    ctrl = _make_controller()
    data = bytes([0, 255])  # only 2 bytes, need 8
    resp = _send_frame(ctrl, Cmd.SET_FILAMENT, data)
    frame = _parse_response(resp)
    assert frame.data[0] == Status.ERROR_INVALID_SLOT


def test_unknown_command():
    ctrl = _make_controller()
    resp = _send_frame(ctrl, 0x7F)  # unknown cmd
    frame = _parse_response(resp)
    assert frame.data[0] == Status.UNKNOWN_CMD


def test_loaded_led_uses_filament_color():
    """Default filament info (green, no material) used when slot transitions to LOADED."""
    ctrl = _make_controller()
    # Slot 3 starts EMPTY with default green (0, 255, 0, b"")
    assert ctrl._filament_info[3] == (0, 255, 0, b"")

    # Trigger sensor → update_from_sensor → LOADED
    ctrl._sensors.sensors[3]._state = True
    ctrl._slots[3].update_from_sensor()
    ctrl._update_slot_led(3)

    assert ctrl._leds._modes[3] == LedMode.SOLID
    assert ctrl._leds._colors[3][:3] == (0, 255, 0)


def test_assign_addr():
    """CMD_ASSIGN_ADDR changes address when UID matches."""
    from micropython_shims.machine import _UNIQUE_ID

    ctrl = _make_controller()
    ctrl._addr = 0xFF  # reset to unassigned

    data = _UNIQUE_ID + bytes([0x03])  # assign address 3
    frame_bytes = build_frame(0xFF, Cmd.ASSIGN_ADDR, 0x01, data)
    parsed = parse_frame(frame_bytes)

    responses = []

    async def _capture(a, c, s, d=b""):
        responses.append(build_response(a, c, s, d))

    ctrl._rs485.send_response = _capture
    asyncio.run(ctrl._handle_frame(parsed))

    assert ctrl._addr == 0x03
    assert len(responses) == 1
    resp_frame = parse_frame(responses[0])
    assert resp_frame.data[0] == Status.OK


def test_assign_addr_wrong_uid():
    """CMD_ASSIGN_ADDR with wrong UID must not change address."""
    ctrl = _make_controller()
    ctrl._addr = 0xFF

    wrong_uid = b"\x00" * 8
    data = wrong_uid + bytes([0x05])
    frame_bytes = build_frame(0xFF, Cmd.ASSIGN_ADDR, 0x02, data)
    parsed = parse_frame(frame_bytes)

    asyncio.run(ctrl._handle_frame(parsed))
    assert ctrl._addr == 0xFF  # unchanged
