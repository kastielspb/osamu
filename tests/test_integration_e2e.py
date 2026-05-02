"""
Layer 3 integration test: End-to-end RS485 bus simulation via in-process pipe.

Architecture:
  ┌─────────────────────────────────┐
  │  "Master" side (test thread)    │   uses build_frame / parse_frame directly
  │  sends commands, reads responses│   over an os.pipe pair
  └───────────┬─────────────────────┘
              │  pipe (bytes)
  ┌───────────▼─────────────────────┐
  │  SlaveController                │   runs asyncio in a background thread
  │  RS485.poll_rx() reads from pipe│   sends responses back via the other pipe
  └─────────────────────────────────┘

Run:  python3 tests/test_integration_e2e.py -v
"""

import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from bus.protocol import Addr, Cmd, LedMode, SlotState, Status  # noqa: E402
from e2e_harness import BusMaster, _load_cfg  # noqa: E402

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_e2e_ping(bus):
    master, slave = bus
    resp = master.query(slave.addr, Cmd.PING)
    assert resp is not None, "PING timed out"
    assert len(resp) == 2, "PING response must be 2 bytes"


def test_e2e_get_status_all_empty(bus):
    master, slave = bus
    resp = master.query(slave.addr, Cmd.GET_STATUS)
    assert resp is not None
    assert len(resp) == 6
    for i in range(4):
        assert resp[i] == SlotState.EMPTY, f"slot {i} not EMPTY"


def test_e2e_discover_assign(make_slave):
    """Full DISCOVER → ASSIGN_ADDR sequence."""
    slave = make_slave()  # unassigned (0xFF)
    master = BusMaster(slave)

    # DISCOVER broadcast
    resp = master.query(Addr.UNASSIGNED, Cmd.DISCOVER, timeout=0.5)
    assert resp is not None and len(resp) == 8, "DISCOVER must return 8-byte UID"
    uid = resp

    # ASSIGN_ADDR
    assign_data = uid + bytes([0x02])
    resp = master.query(Addr.UNASSIGNED, Cmd.ASSIGN_ADDR, assign_data)
    assert resp is not None and resp[0] == Status.OK
    assert slave.ctrl._addr == 0x02


def test_e2e_feed_and_status(bus):
    """Inject filament → FEED → poll GET_STATUS → observe FEEDING state."""
    master, slave = bus
    # Simulate filament present in slot 0
    slave.inject_filament(0, True)
    time.sleep(0.05)  # let sensor_loop debounce

    # Send FEED
    feed_data = struct.pack("<BH", 0, 800)
    resp = master.query(slave.addr, Cmd.FEED, feed_data)
    assert resp is not None, "FEED timed out"
    assert resp[0] == Status.OK, f"FEED returned {resp[0]:#04x}"

    # Poll status
    time.sleep(0.02)
    resp = master.query(slave.addr, Cmd.GET_STATUS)
    assert resp is not None
    assert resp[0] == SlotState.FEEDING, f"Expected FEEDING, got {resp[0]}"


def test_e2e_stop_returns_to_loaded(bus):
    """FEED → STOP → GET_STATUS shows LOADED (sensor still triggered)."""
    master, slave = bus
    slave.inject_filament(1, True)
    time.sleep(0.05)

    feed_data = struct.pack("<BH", 1, 800)
    master.query(slave.addr, Cmd.FEED, feed_data)

    resp = master.query(slave.addr, Cmd.STOP, bytes([1]))
    assert resp is not None and resp[0] == Status.OK

    resp = master.query(slave.addr, Cmd.GET_STATUS)
    assert resp is not None
    assert resp[1] == SlotState.LOADED


def test_e2e_set_filament_color(bus):
    """SET_FILAMENT_COLOR → verify stored color and immediate LED update."""
    master, slave = bus
    # Put slot 2 in LOADED state
    slave.inject_filament(2, True)
    time.sleep(0.05)

    # Set color to orange
    color_data = bytes([2, 255, 128, 0])
    resp = master.query(slave.addr, Cmd.SET_FILAMENT_COLOR, color_data)
    assert resp is not None, "SET_FILAMENT_COLOR timed out"
    assert resp[0] == Status.OK

    # Verify color stored and LED updated
    assert slave.ctrl._filament_colors[2] == (255, 128, 0)
    assert slave.ctrl._leds._modes[2] == LedMode.SOLID
    assert slave.ctrl._leds._colors[2][:3] == (255, 128, 0)


def test_e2e_stop_all_broadcast(bus):
    """STOP_ALL to broadcast address stops all motors (no response expected)."""
    master, slave = bus
    # Feed slots 0 and 1
    for slot in (0, 1):
        slave.inject_filament(slot, True)
    time.sleep(0.05)

    for slot in (0, 1):
        fd = struct.pack("<BH", slot, 800)
        master.query(slave.addr, Cmd.FEED, fd)

    # Broadcast stop
    master.send(Addr.BROADCAST, Cmd.STOP_ALL)
    time.sleep(0.05)

    resp = master.query(slave.addr, Cmd.GET_STATUS)
    assert resp is not None
    # Both slots should not be FEEDING anymore
    assert resp[0] != SlotState.FEEDING
    assert resp[1] != SlotState.FEEDING


def test_e2e_watchdog_triggers_on_silence(bus):
    """After WATCHDOG_TIMEOUT_MS of silence the slave stops all motors."""
    WATCHDOG_TIMEOUT_MS = _load_cfg("WATCHDOG_TIMEOUT_MS")
    master, slave = bus
    # Start feeding slot 0
    slave.inject_filament(0, True)
    time.sleep(0.02)
    feed_data = struct.pack("<BH", 0, 800)
    master.query(slave.addr, Cmd.FEED, feed_data)

    # Go silent long enough for the watchdog to fire.
    # The watchdog_loop checks every 500ms, so we need to wait at least
    # WATCHDOG_TIMEOUT_MS plus one full poll period to guarantee the check
    # occurs AFTER the timeout has elapsed.
    wait_s = (WATCHDOG_TIMEOUT_MS + 700) / 1000.0
    time.sleep(wait_s)

    # Watchdog should have fired — query status
    resp = master.query(slave.addr, Cmd.GET_STATUS)
    assert resp is not None
    # Motor must have been stopped: state is LOADED or ERROR, not FEEDING
    assert resp[0] != SlotState.FEEDING, "Watchdog should have stopped the motor"
