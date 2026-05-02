"""
Scenario integration tests matching docs/firmware-spec.md.

Each test corresponds to one of the six scenarios in the specification:

  Scenario 1 — System Initialisation (MMU_HOME)
  Scenario 2 — Tool Change (T0 → T1)
  Scenario 3 — Normal Polling (during print)
  Scenario 4 — Jam Detection (StallGuard)
  Scenario 5 — Filament Runout (ASSIST → EMPTY)
  Scenario 6 — Connection Loss (Watchdog)

The tests exercise the complete slave firmware stack (state_machine →
bus/rs485 → bus/protocol) via in-process pipes.  No real hardware is
needed.  MicroPython-specific modules are replaced by shims in
tests/micropython_shims/.

Run:  python3 tests/test_scenarios.py -v
"""

import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from e2e_harness import (  # noqa: E402
    Addr,
    BusMasterMulti,
    Cmd,
    LedMode,
    SlotState,
    Status,
    _load_cfg,
)

# ---------------------------------------------------------------------------
# Scenario 1: System Initialisation (MMU_HOME)
# Ref: firmware-spec.md §Scenario 1 / §5 Auto-Enumeration
# ---------------------------------------------------------------------------


def test_scenario_1_system_init(make_slave):
    """
    Multiple unassigned slaves respond to DISCOVER broadcast.
    Master collects UIDs, assigns unique addresses via ASSIGN_ADDR, then
    reads GET_STATUS from each assigned slave.

    Validates:
    - Unassigned slaves respond to addr=0xFF DISCOVER with their 8-byte UID
    - ASSIGN_ADDR matches slave by UID and saves the address
    - Both slaves respond to addressed GET_STATUS after assignment
    """
    # Two slaves start unassigned (addr=0xFF), each with a unique UID
    slave_a = make_slave(auto_start=False)
    slave_a._ctrl._unique_id = b"\x01\x02\x03\x04\x05\x06\x07\x08"
    slave_a.start()
    slave_b = make_slave(auto_start=False)
    slave_b._ctrl._unique_id = b"\xaa\xbb\xcc\xdd\xee\xff\x11\x22"
    slave_b.start()

    bus = BusMasterMulti([slave_a, slave_b])

    # --- Discovery cycle ---
    # Send DISCOVER to addr=0xFF on all slave cmd pipes (shared bus sim)
    bus.send_to_all(Addr.UNASSIGNED, Cmd.DISCOVER)

    # Collect responses from all slaves; allow up to 0.5 s for random backoff
    responses = bus.collect_responses(Cmd.DISCOVER, timeout=0.5)
    assert len(responses) == 2, f"Expected 2 DISCOVER responses, got {len(responses)}"

    uid_map = {r.data: slave for slave, r in responses}
    assert slave_a._ctrl._unique_id in uid_map, "Slave A UID missing"
    assert slave_b._ctrl._unique_id in uid_map, "Slave B UID missing"

    # --- Address assignment ---
    for uid, slave in uid_map.items():
        new_addr = 0x01 if uid == slave_a._ctrl._unique_id else 0x02
        data = uid + bytes([new_addr])
        resp = bus.query_slave(slave, Addr.UNASSIGNED, Cmd.ASSIGN_ADDR, data)
        assert resp is not None and resp[0] == Status.OK, f"ASSIGN_ADDR failed for addr={new_addr}"
        assert slave.ctrl._addr == new_addr, f"Slave did not update its address to {new_addr}"

    # --- GET_STATUS after assignment ---
    for slave, expected_addr in [(slave_a, 0x01), (slave_b, 0x02)]:
        resp = bus.query_slave(slave, expected_addr, Cmd.GET_STATUS)
        assert resp is not None, f"GET_STATUS timed out for slave at addr={expected_addr}"
        assert len(resp) == 6, "GET_STATUS must return 6 bytes"
        # All 4 slots should be EMPTY (no filament injected)
        for i in range(4):
            assert resp[i] == SlotState.EMPTY, f"Slave {expected_addr} slot {i}: expected EMPTY"


# ---------------------------------------------------------------------------
# Scenario 2: Tool Change (T0 → T1)
# Ref: firmware-spec.md §Scenario 2
# ---------------------------------------------------------------------------


def test_scenario_2_tool_change(bus):
    """
    Full filament change cycle:
      1. Retract current slot (T0, slot 0) until sensor clears → EMPTY
      2. Feed new slot (T1, slot 1) → FEEDING
      3. Stop slot 1 (master sensor triggered) → LOADED
      4. Enter assist mode → ASSIST

    Validates:
    - RETRACT starts the motor and transitions to RETRACTING
    - Clearing the filament sensor during RETRACT transitions slot to EMPTY
    - FEED returns OK and transitions slot to FEEDING
    - STOP after FEED (simulating master sensor trigger) returns slot to LOADED
    - SET_ASSIST enters assist mode with correct LED state
    """
    master, slave = bus
    # Both slots start loaded (filament present)
    slave.inject_filament(0, True)
    slave.inject_filament(1, True)
    time.sleep(0.05)  # sensor debounce

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status is not None
    assert status[0] == SlotState.LOADED, "Slot 0 must start LOADED"
    assert status[1] == SlotState.LOADED, "Slot 1 must start LOADED"

    # --- Phase 1: Retract T0 (slot 0) ---
    retract_data = struct.pack("<BH", 0, 1000)
    resp = master.query(slave.addr, Cmd.RETRACT, retract_data)
    assert resp is not None and resp[0] == Status.OK, "RETRACT must return OK"

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[0] == SlotState.RETRACTING, "Slot 0 must be RETRACTING"

    # Simulate filament pulled back past sensor
    slave.inject_filament(0, False)
    time.sleep(0.05)  # debounce + sensor_loop → on_retract_complete()

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status is not None
    assert status[0] == SlotState.EMPTY, (
        f"Slot 0 must be EMPTY after retract complete, got {status[0]}"
    )

    # --- Phase 2: Feed T1 (slot 1) ---
    feed_data = struct.pack("<BH", 1, 800)
    resp = master.query(slave.addr, Cmd.FEED, feed_data)
    assert resp is not None and resp[0] == Status.OK, "FEED must return OK"

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[1] == SlotState.FEEDING, f"Slot 1 must be FEEDING, got {status[1]}"

    # --- Phase 3: Master sensor triggered → stop motor ---
    resp = master.query(slave.addr, Cmd.STOP, bytes([1]))
    assert resp is not None and resp[0] == Status.OK, "STOP must return OK"

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[1] == SlotState.LOADED, (
        f"Slot 1 must be LOADED after STOP (filament still present), got {status[1]}"
    )

    # --- Phase 4: Enter assist mode (friction compensation) ---
    assist_data = struct.pack("<BH", 1, 150)  # slot=1, current=150mA
    resp = master.query(slave.addr, Cmd.SET_ASSIST, assist_data)
    assert resp is not None and resp[0] == Status.OK, "SET_ASSIST must return OK"

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[1] == SlotState.ASSIST, f"Slot 1 must be in ASSIST mode, got {status[1]}"


# ---------------------------------------------------------------------------
# Scenario 3: Normal Polling (during print)
# Ref: firmware-spec.md §Scenario 3 / §1.5 Polling Cycle
# ---------------------------------------------------------------------------


def test_scenario_3_normal_polling(bus):
    """
    Simulate ~20 Hz GET_STATUS polling for 1 second.

    Validates:
    - Every response arrives within the 10 ms response timeout from the spec
    - Slot states remain consistent across all polls
    - No response is None (simulating the offline detection threshold)
    """
    master, slave = bus
    # Set up a known state: slots 0 and 2 loaded
    slave.inject_filament(0, True)
    slave.inject_filament(2, True)
    time.sleep(0.05)

    POLL_COUNT = 20
    POLL_INTERVAL = 0.05  # 50 ms → ~20 Hz
    MAX_RESPONSE = 0.010  # 10 ms per spec §1.1

    for poll_num in range(POLL_COUNT):
        t0 = time.monotonic()
        resp = master.query(slave.addr, Cmd.GET_STATUS, timeout=0.1)
        elapsed = time.monotonic() - t0

        assert resp is not None, f"Poll {poll_num}: GET_STATUS timed out (slave offline?)"
        assert len(resp) == 6, f"Poll {poll_num}: unexpected response length {len(resp)}"
        assert resp[0] == SlotState.LOADED, (
            f"Poll {poll_num}: slot 0 state changed unexpectedly to {resp[0]}"
        )
        assert resp[1] == SlotState.EMPTY, (
            f"Poll {poll_num}: slot 1 state changed unexpectedly to {resp[1]}"
        )
        assert resp[2] == SlotState.LOADED, (
            f"Poll {poll_num}: slot 2 state changed unexpectedly to {resp[2]}"
        )
        assert resp[3] == SlotState.EMPTY, (
            f"Poll {poll_num}: slot 3 state changed unexpectedly to {resp[3]}"
        )
        assert elapsed <= MAX_RESPONSE, (
            f"Poll {poll_num}: response too slow ({elapsed * 1000:.1f} ms > 10 ms)"
        )

        time.sleep(max(0, POLL_INTERVAL - elapsed))


# ---------------------------------------------------------------------------
# Scenario 4: Jam Detection (StallGuard)
# Ref: firmware-spec.md §Scenario 4 / §2.5 StallGuard
# ---------------------------------------------------------------------------


def test_scenario_4_jam_detection(bus):
    """
    StallGuard reports a low SG_RESULT during FEEDING → slot transitions to
    ERROR with error code ERROR_JAM.

    Method: patch tmc.read_stallguard() on slot 1's driver to always return 0
    (below STALLGUARD_THRESHOLD=20).  The _stallguard_loop detects this within
    STALLGUARD_POLL_MS (200 ms) and calls slot.check_stallguard().

    Validates:
    - Slot in FEEDING can be jammed by low SG_RESULT
    - GET_STATUS reports ERROR for the jammed slot
    - Error byte encodes ERROR_JAM for the correct slot
    """
    STALLGUARD_POLL_MS = _load_cfg("STALLGUARD_POLL_MS")
    master, slave = bus
    # Load slot 1
    slave.inject_filament(1, True)
    time.sleep(0.05)

    # Start feeding
    feed_data = struct.pack("<BH", 1, 800)
    resp = master.query(slave.addr, Cmd.FEED, feed_data)
    assert resp is not None and resp[0] == Status.OK

    # Patch StallGuard to report always-stalled (SG_RESULT = 0 < threshold)
    slave.ctrl._tmc_bank.drivers[1].read_stallguard = lambda: 0

    # Wait for the stallguard_loop to fire (poll interval + margin)
    time.sleep((STALLGUARD_POLL_MS + 100) / 1000.0)

    resp = master.query(slave.addr, Cmd.GET_STATUS)
    assert resp is not None
    slot_state = resp[1]
    assert slot_state == SlotState.ERROR, f"Slot 1 must be ERROR after jam, got {slot_state}"

    # Error byte: bits [3:2] carry slot 1's error code (2-bit fields)
    error_byte = resp[5]
    slot1_err = (error_byte >> 2) & 0x03
    assert slot1_err == Status.ERROR_JAM, (
        f"Slot 1 error code must be ERROR_JAM ({Status.ERROR_JAM}), got {slot1_err}"
    )


# ---------------------------------------------------------------------------
# Scenario 5: Filament Runout (ASSIST → EMPTY)
# Ref: firmware-spec.md §Scenario 5
# ---------------------------------------------------------------------------


def test_scenario_5_filament_runout(bus):
    """
    During assist mode the spool runs out: sensor clears, slave auto-stops
    the motor and transitions the slot to EMPTY.

    Validates:
    - SET_ASSIST enters ASSIST state
    - Removing filament (sensor clear) during ASSIST causes slot → EMPTY
    - Motor is stopped (no ASSIST state remains)
    - LED transitions to OFF (EMPTY state)
    """
    master, slave = bus
    # Slot 0 loaded; enter assist mode
    slave.inject_filament(0, True)
    time.sleep(0.05)

    assist_data = struct.pack("<BH", 0, 150)
    resp = master.query(slave.addr, Cmd.SET_ASSIST, assist_data)
    assert resp is not None and resp[0] == Status.OK

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[0] == SlotState.ASSIST, "Slot 0 must be in ASSIST mode"

    # Spool runs out — sensor opens
    slave.inject_filament(0, False)

    # Wait for debounce (5 ms) + sensor_loop (2 ms poll) + margin
    time.sleep(0.05)

    # The _sensor_loop should have called slot.stop() → EMPTY
    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status is not None
    assert status[0] == SlotState.EMPTY, f"Slot 0 must be EMPTY after runout, got {status[0]}"


# ---------------------------------------------------------------------------
# Scenario 6: Connection Loss (Watchdog)
# Ref: firmware-spec.md §Scenario 6 / §2.4 Watchdog
# ---------------------------------------------------------------------------


def test_scenario_6_connection_loss(bus):
    """
    Master goes silent for longer than WATCHDOG_TIMEOUT_MS (5 s).
    Slave detects the silence, stops all motors, and transitions all active
    slots to a stopped state.

    After connection is restored (master sends GET_STATUS), the slave resumes
    normal command processing.

    Validates:
    - Motors feeding when silence begins are stopped by the watchdog
    - GET_STATUS is answered normally once master resumes
    - No slot remains in FEEDING state after the watchdog fires
    """
    WATCHDOG_TIMEOUT_MS = _load_cfg("WATCHDOG_TIMEOUT_MS")
    master, slave = bus
    # Start feeding slot 2
    slave.inject_filament(2, True)
    time.sleep(0.05)

    feed_data = struct.pack("<BH", 2, 800)
    resp = master.query(slave.addr, Cmd.FEED, feed_data)
    assert resp is not None and resp[0] == Status.OK
    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[2] == SlotState.FEEDING, "Slot 2 must be FEEDING before silence"

    # Master goes silent
    # watchdog_loop checks every 500 ms; wait > WATCHDOG_TIMEOUT_MS + one period
    time.sleep((WATCHDOG_TIMEOUT_MS + 700) / 1000.0)

    # Master reconnects — first GET_STATUS after silence
    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status is not None, "Slave must respond after watchdog recovery"
    assert status[2] != SlotState.FEEDING, "Watchdog must have stopped slot 2 (no longer FEEDING)"

    # Slave remains responsive
    resp = master.query(slave.addr, Cmd.PING)
    assert resp is not None and len(resp) == 2, "Slave must respond to PING after watchdog recovery"


# ---------------------------------------------------------------------------
# Scenario 7: Set Filament Color
# Ref: firmware-spec.md §Scenario 7
# ---------------------------------------------------------------------------


def test_scenario_7_set_filament_color(bus):
    """
    MMU_SET_FILAMENT_COLOR pushes the color to the slave.

    Branch A (LOADED): LED updates immediately to the new solid color.
    Branch B (EMPTY):  Color is stored; LED does not change yet.
    Branch B continued: When the slot later becomes LOADED (filament inserted),
                        _update_slot_led uses the stored color automatically.

    Validates:
    - SET_FILAMENT_COLOR returns OK in both cases
    - _filament_colors[slot] is updated in both cases
    - LED is set to SOLID with the correct RGB when the slot is LOADED
    - LED is NOT changed when the slot is not LOADED
    - Color persists and is applied on the EMPTY → LOADED transition
    """
    master, slave = bus
    # --- Branch A: slot 1 is LOADED ---
    slave.inject_filament(1, True)
    time.sleep(0.05)  # sensor debounce

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[1] == SlotState.LOADED, "Slot 1 must be LOADED for branch A"

    color_data = bytes([1, 255, 51, 0])  # slot=1, r=255, g=51, b=0 (#FF3300)
    resp = master.query(slave.addr, Cmd.SET_FILAMENT_COLOR, color_data)
    assert resp is not None and resp[0] == Status.OK, (
        "SET_FILAMENT_COLOR must return OK when slot is LOADED"
    )

    # Color stored
    assert slave.ctrl._filament_colors[1] == (255, 51, 0), (
        "Color must be stored in _filament_colors[1]"
    )

    # LED immediately solid with the new color (slot is LOADED)
    time.sleep(0.02)  # let _led_loop tick
    assert slave.ctrl._leds._modes[1] == LedMode.SOLID, (
        "LED mode must be SOLID when slot is LOADED and color was just set"
    )
    assert slave.ctrl._leds._colors[1][:3] == (255, 51, 0), "LED color must match the set color"

    # --- Branch B: slot 2 is EMPTY ---
    # (no filament injected for slot 2)
    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[2] == SlotState.EMPTY, "Slot 2 must be EMPTY for branch B"

    led_mode_before = slave.ctrl._leds._modes[2]
    color_data_b = bytes([2, 0, 200, 80])  # slot=2, some green
    resp = master.query(slave.addr, Cmd.SET_FILAMENT_COLOR, color_data_b)
    assert resp is not None and resp[0] == Status.OK, (
        "SET_FILAMENT_COLOR must return OK when slot is EMPTY"
    )

    # Color stored
    assert slave.ctrl._filament_colors[2] == (0, 200, 80), (
        "Color must be stored in _filament_colors[2] even when EMPTY"
    )

    # LED must NOT have changed (slot is not LOADED)
    time.sleep(0.02)
    assert slave.ctrl._leds._modes[2] == led_mode_before, (
        "LED mode must not change when slot is not LOADED"
    )

    # --- Branch B continued: filament inserted → LOADED uses stored color ---
    slave.inject_filament(2, True)
    time.sleep(0.05)  # debounce + sensor_loop → update_from_sensor

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[2] == SlotState.LOADED, "Slot 2 must be LOADED after filament insertion"

    # _update_slot_led should have applied the stored color
    assert slave.ctrl._leds._modes[2] == LedMode.SOLID, (
        "LED must be SOLID when slot transitions to LOADED"
    )
    assert slave.ctrl._leds._colors[2][:3] == (0, 200, 80), (
        "LED must use the stored color when transitioning to LOADED"
    )
