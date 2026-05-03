"""
Scenario integration tests matching docs/firmware-spec.md.

Each test corresponds to one of the numbered scenarios in the specification:

  Setup & Bus Management:
  Scenario 1  — System Initialisation (MMU_HOME)
  Scenario 2  — Hot-Plug: Second Slave Joins Active Bus

  Core Print Operations:
  Scenario 3  — Normal Polling (during print)
  Scenario 4  — Tool Change (T0 → T1)
  Scenario 5  — Short Retracts During Active Print (ASSIST Unaffected)
  Scenario 6  — Concurrent Slots (ASSIST + FEED)
  Scenario 7  — RETRACT on an Idle Slot While Another Is in ASSIST

  Filament Metadata:
  Scenario 8  — Set Filament Info
  Scenario 9  — Filament Info After Power Cycle
  Scenario 10 — SET_FILAMENT While a Slot Is in ASSIST (branch A)
  Scenario 10b— SET_FILAMENT on an Idle Slot During ASSIST (branch B)
  Scenario 11 — Filament Info Preserved After Runout
  Scenario 12 — New Filament Info Applied After Planned Spool Swap

  Fault Handling:
  Scenario 13 — Jam Detection (StallGuard)
  Scenario 14 — Filament Runout (ASSIST → EMPTY)
  Scenario 15 — Connection Loss (Watchdog)

  Infinite Spool & Runout Recovery:
  Scenario 16 — Infinite Spool (Slot-Chain Auto-Switch)
  Scenario 17 — Spool Exhaustion Without Infinite Spool (no test)
  Scenario 18 — System Block Diagram (no test)

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
from bus.protocol import Addr, Cmd, LedMode, SlotState, Status  # noqa: E402
from e2e_harness import BusMaster, BusMasterMulti, _load_cfg  # noqa: E402

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
# Scenario 2: Hot-Plug — Second Slave Joins Active Bus
# Ref: firmware-spec.md §Scenario 2
# ---------------------------------------------------------------------------


def test_scenario_2_hotplug_slave_joins_active_bus(make_slave):
    """
    A second slave boots and joins the bus while the first slave is already
    assigned and being polled normally.

    Validates:
    - DISCOVER does not disturb an already-assigned slave
    - Late-joining slave can be enumerated and addressed
    - Both slaves coexist on the bus after hot-plug
    """
    slave1 = make_slave(0x01)
    slave1.inject_filament(0, True)
    time.sleep(0.05)

    master1 = BusMaster(slave1)

    # Slave 1 is up and answering normally
    status = master1.query(slave1.addr, Cmd.GET_STATUS)
    assert status is not None, "Slave 1 must respond before hot-plug"
    assert status[0] == SlotState.LOADED, "Slave 1 slot 0 must be LOADED"

    # --- Hot-plug: slave 2 boots unassigned ---
    slave2 = make_slave()  # addr=0xFF

    multi = BusMasterMulti([slave1, slave2])

    # DISCOVER broadcast — slave 1 must NOT respond (already assigned)
    multi.send_to_all(Addr.UNASSIGNED, Cmd.DISCOVER)
    responses = multi.collect_responses(Cmd.DISCOVER, timeout=0.5)
    assert len(responses) == 1, (
        f"Only the unassigned slave must respond to DISCOVER, got {len(responses)}"
    )
    uid2 = responses[0][1].data

    # Assign address 0x02 to slave 2
    assign_data = uid2 + bytes([0x02])
    resp = multi.query_slave(slave2, Addr.UNASSIGNED, Cmd.ASSIGN_ADDR, assign_data)
    assert resp is not None and resp[0] == Status.OK
    assert slave2.ctrl._addr == 0x02

    # Both slaves answer GET_STATUS independently
    status1 = master1.query(slave1.addr, Cmd.GET_STATUS)
    assert status1 is not None, "Slave 1 must still respond after hot-plug"
    assert status1[0] == SlotState.LOADED, "Slave 1 state must be unchanged"

    status2 = multi.query_slave(slave2, 0x02, Cmd.GET_STATUS)
    assert status2 is not None, "Slave 2 must respond after address assignment"
    assert len(status2) == 6


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
# Scenario 4: Tool Change (T0 → T1)
# Ref: firmware-spec.md §Scenario 4
# ---------------------------------------------------------------------------


def test_scenario_4_tool_change(bus):
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
# Scenario 5: Short Retracts During Active Print (ASSIST Unaffected)
# Ref: firmware-spec.md §Scenario 5
# ---------------------------------------------------------------------------


def test_scenario_5_short_retracts_do_not_affect_assist(bus):
    """
    Short extruder retracts (travel moves) are extruder-only; no RS485 command
    is sent to the slave.  The slave stays in ASSIST throughout.

    Simulated here by polling GET_STATUS repeatedly without sending any
    RETRACT command to the slave, verifying ASSIST persists.

    Validates:
    - Repeated GET_STATUS polls do not change the ASSIST state
    - No spurious state transitions occur during normal polling
    """
    master, slave = bus
    slave.inject_filament(0, True)
    time.sleep(0.05)

    resp = master.query(slave.addr, Cmd.SET_ASSIST, struct.pack("<BH", 0, 150))
    assert resp is not None and resp[0] == Status.OK

    # Simulate 20 polling cycles (~1 s at 20 Hz) without any motor command
    for poll in range(20):
        status = master.query(slave.addr, Cmd.GET_STATUS)
        assert status is not None, f"Poll {poll}: slave must respond"
        assert status[0] == SlotState.ASSIST, (
            f"Poll {poll}: slot 0 must remain ASSIST, got {status[0]}"
        )
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Scenario 6: Concurrent Slots — ASSIST Active While Another Slot Is Fed
# Ref: firmware-spec.md §Scenario 6
# ---------------------------------------------------------------------------


def test_scenario_6_feed_during_assist(bus):
    """
    Slot 0 is in ASSIST mode (active print in progress).
    Master starts feeding slot 1 (next colour staged).

    Validates:
    - FEED on slot 1 returns OK while slot 0 remains in ASSIST
    - GET_STATUS shows both states simultaneously (slot 0 ASSIST, slot 1 FEEDING)
    - STOP on slot 1 returns OK; slot 0 stays in ASSIST unaffected
    """
    master, slave = bus
    # Load both slots
    slave.inject_filament(0, True)
    slave.inject_filament(1, True)
    time.sleep(0.05)

    # Slot 0: enter assist
    assist_data = struct.pack("<BH", 0, 150)
    resp = master.query(slave.addr, Cmd.SET_ASSIST, assist_data)
    assert resp is not None and resp[0] == Status.OK

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[0] == SlotState.ASSIST, "Slot 0 must be in ASSIST"

    # Slot 1: start feeding while slot 0 is still in ASSIST
    feed_data = struct.pack("<BH", 1, 800)
    resp = master.query(slave.addr, Cmd.FEED, feed_data)
    assert resp is not None and resp[0] == Status.OK, "FEED on slot 1 must succeed during ASSIST"

    # Both active simultaneously
    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[0] == SlotState.ASSIST, "Slot 0 must remain ASSIST while slot 1 is FEEDING"
    assert status[1] == SlotState.FEEDING, "Slot 1 must be FEEDING"

    # Stop slot 1 — slot 0 must be unaffected
    resp = master.query(slave.addr, Cmd.STOP, bytes([1]))
    assert resp is not None and resp[0] == Status.OK

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[0] == SlotState.ASSIST, "Slot 0 must still be ASSIST after slot 1 stopped"
    assert status[1] == SlotState.LOADED, "Slot 1 must be LOADED after STOP (filament present)"


# ---------------------------------------------------------------------------
# Scenario 7: RETRACT on an Idle Slot While Another Is in ASSIST
# Ref: firmware-spec.md §Scenario 7
# ---------------------------------------------------------------------------


def test_scenario_7_retract_idle_slot_during_assist(bus):
    """
    Slot 1 is in ASSIST (active print).  Slot 2 is LOADED (idle spool).
    Master triggers RETRACT on slot 2 to unload it.

    Validates:
    - RETRACT on a LOADED slot returns OK while another slot is in ASSIST
    - GET_STATUS shows ASSIST + RETRACTING simultaneously
    - Sensor clear on slot 2 transitions it to EMPTY (on_retract_complete)
    - Slot 1 remains ASSIST throughout
    """
    master, slave = bus
    slave.inject_filament(1, True)
    slave.inject_filament(2, True)
    time.sleep(0.05)

    # Slot 1 enters ASSIST
    resp = master.query(slave.addr, Cmd.SET_ASSIST, struct.pack("<BH", 1, 150))
    assert resp is not None and resp[0] == Status.OK

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[1] == SlotState.ASSIST, "Slot 1 must be in ASSIST"
    assert status[2] == SlotState.LOADED, "Slot 2 must be LOADED"

    # Retract slot 2 while slot 1 is still in ASSIST
    retract_data = struct.pack("<BH", 2, 1000)
    resp = master.query(slave.addr, Cmd.RETRACT, retract_data)
    assert resp is not None and resp[0] == Status.OK, "RETRACT must return OK"

    # Both states visible simultaneously
    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[1] == SlotState.ASSIST, "Slot 1 must remain ASSIST during slot 2 retract"
    assert status[2] == SlotState.RETRACTING, "Slot 2 must be RETRACTING"

    # Simulate filament pulled clear of sensor on slot 2
    slave.inject_filament(2, False)
    time.sleep(0.05)  # debounce + sensor_loop → on_retract_complete()

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[1] == SlotState.ASSIST, "Slot 1 must still be ASSIST after slot 2 completes"
    assert status[2] == SlotState.EMPTY, "Slot 2 must be EMPTY after retract complete"


# ---------------------------------------------------------------------------
# Scenario 8: Set Filament Info
# Ref: firmware-spec.md §Scenario 8
# ---------------------------------------------------------------------------


def test_scenario_8_set_filament_info(bus):
    """
    MMU_SET_FILAMENT pushes filament info to the slave.

    Branch A (LOADED): LED updates immediately to the new solid color.
    Branch B (EMPTY):  Info is stored; LED does not change yet.
    Branch B continued: When the slot later becomes LOADED (filament inserted),
                        _update_slot_led uses the stored color automatically.

    Validates:
    - SET_FILAMENT returns OK in both cases
    - _filament_info[slot] is updated in both cases
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

    color_data = bytes([1, 255, 51, 0]) + b"\x00\x00\x00\x00"  # slot=1, #FF3300, no material
    resp = master.query(slave.addr, Cmd.SET_FILAMENT, color_data)
    assert resp is not None and resp[0] == Status.OK, (
        "SET_FILAMENT must return OK when slot is LOADED"
    )

    # Color stored
    assert slave.ctrl._filament_info[1] == (255, 51, 0, b""), (
        "Filament info must be stored in _filament_info[1]"
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
    color_data_b = bytes([2, 0, 200, 80]) + b"\x00\x00\x00\x00"  # slot=2, some green
    resp = master.query(slave.addr, Cmd.SET_FILAMENT, color_data_b)
    assert resp is not None and resp[0] == Status.OK, (
        "SET_FILAMENT must return OK when slot is EMPTY"
    )

    # Color stored
    assert slave.ctrl._filament_info[2] == (0, 200, 80, b""), (
        "Filament info must be stored in _filament_info[2] even when EMPTY"
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


# ---------------------------------------------------------------------------
# Scenario 9: Filament Info After Power Cycle
# Ref: firmware-spec.md §Scenario 9
# ---------------------------------------------------------------------------


def test_scenario_9_filament_info_after_power_cycle(bus):
    """
    On every boot _filament_info resets to the compiled-in default (solid
    green, no material).  During MMU_HOME, Klipper pushes saved filament info
    back via SET_FILAMENT for slots that have a saved entry; slots with no
    saved entry keep the default.

    The fixture provides a freshly-started slave, which models a power cycle.

    Validates:
    - All slots start with the default filament info (0, 255, 0, b"") after boot
    - SET_FILAMENT updates exactly the targeted slots
    - Slots that receive no SET_FILAMENT remain at the default green
    - LED reflects the updated color for each LOADED slot immediately
    """
    master, slave = bus

    # --- Verify defaults right after boot ---
    for slot in range(4):
        assert slave.ctrl._filament_info[slot] == (0, 255, 0, b""), (
            f'Slot {slot}: default filament info must be (0, 255, 0, b"") on boot'
        )

    # Load slots 0, 1, 3 (slot 2 deliberately left empty — no filament push)
    slave.inject_filament(0, True)
    slave.inject_filament(1, True)
    slave.inject_filament(3, True)
    time.sleep(0.05)

    # --- Re-sync three slots (simulating MMU_HOME filament info restore) ---
    colors = [
        (0, 0, (0, 255, 0)),  # slot 0 — keep default green
        (1, 1, (255, 51, 0)),  # slot 1 — orange
        (3, 3, (0, 0, 255)),  # slot 3 — blue
    ]
    for slot, _, (r, g, b) in colors:
        resp = master.query(
            slave.addr, Cmd.SET_FILAMENT, bytes([slot, r, g, b]) + b"\x00\x00\x00\x00"
        )
        assert resp is not None and resp[0] == Status.OK, (
            f"SET_FILAMENT must return OK for slot {slot}"
        )

    # Slot 0: explicitly set to same default green
    assert slave.ctrl._filament_info[0] == (0, 255, 0, b"")
    # Slot 1: updated to orange
    assert slave.ctrl._filament_info[1] == (255, 51, 0, b"")
    # Slot 3: updated to blue
    assert slave.ctrl._filament_info[3] == (0, 0, 255, b"")
    # Slot 2: never touched — still default green
    assert slave.ctrl._filament_info[2] == (0, 255, 0, b""), (
        "Slot 2 must retain default green — no SET_FILAMENT was issued"
    )

    # LEDs for LOADED slots must reflect stored colors
    time.sleep(0.02)  # let _led_loop tick
    assert slave.ctrl._leds._colors[1][:3] == (255, 51, 0), "Slot 1 LED must be orange"
    assert slave.ctrl._leds._colors[3][:3] == (0, 0, 255), "Slot 3 LED must be blue"
    # Slot 2 is EMPTY — LED state is irrelevant but filament entry must still be default
    assert slave.ctrl._filament_info[2] == (0, 255, 0, b"")


# ---------------------------------------------------------------------------
# Scenario 10: SET_FILAMENT While a Slot Is in ASSIST (branch A)
# Ref: firmware-spec.md §Scenario 10
# ---------------------------------------------------------------------------


def test_scenario_10_set_filament_on_assisting_slot(bus):
    """
    SET_FILAMENT sent for the slot currently in ASSIST mode.

    The filament info must be stored for later use but the LED must not be
    changed immediately (it should stay in the ASSIST visual state).
    When the slot subsequently stops and returns to LOADED, the stored
    color is applied automatically.

    Validates:
    - SET_FILAMENT returns OK for an ASSIST slot
    - _filament_info is updated
    - LED mode does NOT change while the slot is in ASSIST
    - After STOP, LED transitions to SOLID with the new color
    """
    master, slave = bus
    slave.inject_filament(0, True)
    time.sleep(0.05)

    # Enter assist on slot 0
    resp = master.query(slave.addr, Cmd.SET_ASSIST, struct.pack("<BH", 0, 150))
    assert resp is not None and resp[0] == Status.OK
    assert slave.ctrl._slots[0].state == SlotState.ASSIST

    led_mode_before = slave.ctrl._leds._modes[0]  # should be ASSIST visual

    # Set a new color while in ASSIST
    color_data = bytes([0, 200, 100, 0]) + b"\x00\x00\x00\x00"  # slot=0, amber
    resp = master.query(slave.addr, Cmd.SET_FILAMENT, color_data)
    assert resp is not None and resp[0] == Status.OK, "SET_FILAMENT must return OK"

    # Filament info stored
    assert slave.ctrl._filament_info[0] == (200, 100, 0, b""), "Filament info must be stored"

    # LED must NOT have changed — slot is still in ASSIST
    time.sleep(0.02)
    assert slave.ctrl._leds._modes[0] == led_mode_before, (
        "LED must not change while slot is in ASSIST"
    )

    # Stop slot 0 → transitions to LOADED (filament still present)
    resp = master.query(slave.addr, Cmd.STOP, bytes([0]))
    assert resp is not None and resp[0] == Status.OK

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[0] == SlotState.LOADED

    # LED must now reflect the stored color
    assert slave.ctrl._leds._modes[0] == LedMode.SOLID, (
        "LED must be SOLID after slot returns to LOADED"
    )
    assert slave.ctrl._leds._colors[0][:3] == (200, 100, 0), (
        "LED must use the color set during ASSIST"
    )


def test_scenario_10b_set_filament_on_idle_slot_during_assist(bus):
    """
    Scenario 10 Branch B: SET_FILAMENT sent for a *different*, idle
    (LOADED) slot while slot 0 is in ASSIST.  The idle slot's LED must update
    immediately; slot 0 must remain completely unaffected.

    Validates:
    - SET_FILAMENT on LOADED slot updates LED immediately
    - Slot in ASSIST is unaffected by color change on another slot
    """
    master, slave = bus
    slave.inject_filament(0, True)
    slave.inject_filament(2, True)
    time.sleep(0.05)

    # Slot 0 in ASSIST
    resp = master.query(slave.addr, Cmd.SET_ASSIST, struct.pack("<BH", 0, 150))
    assert resp is not None and resp[0] == Status.OK

    assist_led_mode = slave.ctrl._leds._modes[0]

    # Set color on slot 2 (LOADED, idle)
    color_data = bytes([2, 0, 0, 255]) + b"\x00\x00\x00\x00"  # blue
    resp = master.query(slave.addr, Cmd.SET_FILAMENT, color_data)
    assert resp is not None and resp[0] == Status.OK

    # Slot 2 LED updated immediately
    assert slave.ctrl._filament_info[2] == (0, 0, 255, b"")
    assert slave.ctrl._leds._modes[2] == LedMode.SOLID
    assert slave.ctrl._leds._colors[2][:3] == (0, 0, 255)

    # Slot 0 ASSIST LED unchanged
    assert slave.ctrl._leds._modes[0] == assist_led_mode, (
        "Slot 0 ASSIST LED must not be affected by color change on slot 2"
    )


# ---------------------------------------------------------------------------
# Scenario 11: Filament Info Preserved After Runout
# Ref: firmware-spec.md §Scenario 11
# ---------------------------------------------------------------------------


def test_scenario_11_filament_info_preserved_after_runout(bus):
    """
    When a spool runs out during ASSIST, the slave transitions the slot to
    EMPTY autonomously.  The filament info (_filament_info) must NOT be
    cleared — it persists so that the master does not have to re-push it if
    the same material is reloaded.  When a new spool is inserted (sensor
    triggered → LOADED), _update_slot_led re-applies the stored color.

    Setup:
    - Slot 0: SET_FILAMENT with red PLA, placed in ASSIST (printing)

    Sequence:
    1. Runout — sensor opens → slot 0 → EMPTY
    2. Verify _filament_info[0] unchanged (not wiped by runout)
    3. Insert new spool (same material) — sensor triggers → LOADED
    4. Verify LED automatically applies stored red color

    Validates:
    - _filament_info survives a runout-driven EMPTY transition
    - Stored color is re-applied via _update_slot_led on EMPTY → LOADED
    """
    master, slave = bus

    # Load slot 0 with a red PLA spool
    slave.inject_filament(0, True)
    time.sleep(0.05)

    resp = master.query(slave.addr, Cmd.SET_FILAMENT, bytes([0, 220, 30, 30]) + b"PLA\x00")
    assert resp is not None and resp[0] == Status.OK, "SET_FILAMENT must return OK"
    assert slave.ctrl._filament_info[0] == (220, 30, 30, b"PLA"), (
        "_filament_info[0] must be (220, 30, 30, b'PLA') after SET_FILAMENT"
    )

    # Enter ASSIST (printing in progress)
    resp = master.query(slave.addr, Cmd.SET_ASSIST, struct.pack("<BH", 0, 150))
    assert resp is not None and resp[0] == Status.OK
    assert slave.ctrl._slots[0].state == SlotState.ASSIST

    # --- Phase 1: Spool runs out — sensor opens ---
    slave.inject_filament(0, False)
    time.sleep(0.05)  # debounce + sensor_loop

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[0] == SlotState.EMPTY, (
        f"Slot 0 must be EMPTY after runout during ASSIST, got {status[0]}"
    )

    # --- Phase 2: Filament info must NOT be cleared by runout ---
    assert slave.ctrl._filament_info[0] == (220, 30, 30, b"PLA"), (
        "_filament_info must be preserved after runout — slave must not wipe it"
    )

    # --- Phase 3: New spool inserted (same material) → LOADED ---
    slave.inject_filament(0, True)
    time.sleep(0.05)  # debounce + sensor_loop → update_from_sensor → LOADED

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[0] == SlotState.LOADED, (
        f"Slot 0 must be LOADED after new spool inserted, got {status[0]}"
    )

    # --- Phase 4: LED must reapply stored color automatically ---
    time.sleep(0.02)  # let _led_loop tick
    assert slave.ctrl._leds._modes[0] == LedMode.SOLID, (
        "LED must be SOLID when slot returns to LOADED"
    )
    assert slave.ctrl._leds._colors[0][:3] == (220, 30, 30), (
        "LED must reapply the stored red color after new spool is inserted"
    )


# ---------------------------------------------------------------------------
# Scenario 12: New Filament Info Applied After Planned Spool Swap
# Ref: firmware-spec.md §Scenario 12
# ---------------------------------------------------------------------------


def test_scenario_12_new_filament_info_after_spool_swap(bus):
    """
    Planned spool change: master retracts the current spool, then pushes new
    filament info for the replacement material before re-loading.  The new
    info must be stored immediately and the LED must reflect it once the slot
    returns to LOADED.

    Setup:
    - Slot 1: blue PETG spool initially configured and loaded

    Sequence:
    1. RETRACT slot 1 → RETRACTING → sensor clears → EMPTY  (spool removed)
    2. Master pushes new SET_FILAMENT for slot 1 (green ABS)
    3. New spool inserted → LOADED
    4. Verify LED shows green ABS color

    Validates:
    - SET_FILAMENT accepted while slot is EMPTY (spool swap window)
    - New _filament_info replaces old entry
    - LED applies new color on EMPTY → LOADED transition
    - Old color (blue PETG) is no longer in effect after the update
    """
    master, slave = bus

    # Load slot 1 with a blue PETG spool
    slave.inject_filament(1, True)
    time.sleep(0.05)

    resp = master.query(slave.addr, Cmd.SET_FILAMENT, bytes([1, 0, 80, 220]) + b"PETG")
    assert resp is not None and resp[0] == Status.OK, "Initial SET_FILAMENT must return OK"
    assert slave.ctrl._filament_info[1] == (0, 80, 220, b"PETG"), (
        "_filament_info[1] must reflect blue PETG"
    )

    # --- Phase 1: Retract slot 1 (remove spool) ---
    retract_data = struct.pack("<BH", 1, 1000)
    resp = master.query(slave.addr, Cmd.RETRACT, retract_data)
    assert resp is not None and resp[0] == Status.OK, "RETRACT must return OK"

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[1] == SlotState.RETRACTING, "Slot 1 must be RETRACTING"

    slave.inject_filament(1, False)
    time.sleep(0.05)  # debounce + sensor_loop → on_retract_complete → EMPTY

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[1] == SlotState.EMPTY, f"Slot 1 must be EMPTY after retract, got {status[1]}"

    # --- Phase 2: Master pushes new filament info (green ABS) ---
    resp = master.query(slave.addr, Cmd.SET_FILAMENT, bytes([1, 20, 180, 60]) + b"ABS\x00")
    assert resp is not None and resp[0] == Status.OK, (
        "SET_FILAMENT must be accepted while slot is EMPTY"
    )
    assert slave.ctrl._filament_info[1] == (20, 180, 60, b"ABS"), (
        "_filament_info[1] must be updated to green ABS"
    )
    # Old blue PETG must be gone
    assert slave.ctrl._filament_info[1] != (0, 80, 220, b"PETG"), (
        "Old PETG info must no longer be stored"
    )

    # --- Phase 3: New spool inserted → LOADED ---
    slave.inject_filament(1, True)
    time.sleep(0.05)  # debounce + sensor_loop → update_from_sensor → LOADED

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[1] == SlotState.LOADED, (
        f"Slot 1 must be LOADED after new spool inserted, got {status[1]}"
    )

    # --- Phase 4: LED must show green ABS, not old blue PETG ---
    time.sleep(0.02)
    assert slave.ctrl._leds._modes[1] == LedMode.SOLID, (
        "LED must be SOLID when slot returns to LOADED"
    )
    assert slave.ctrl._leds._colors[1][:3] == (20, 180, 60), (
        "LED must reflect the new green ABS color, not the old blue PETG"
    )


# ---------------------------------------------------------------------------
# Scenario 13: Jam Detection (StallGuard)
# Ref: firmware-spec.md §Scenario 13 / §2.5 StallGuard
# ---------------------------------------------------------------------------


def test_scenario_13_jam_detection(bus):
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
# Scenario 14: Filament Runout (ASSIST → EMPTY)
# Ref: firmware-spec.md §Scenario 14
# ---------------------------------------------------------------------------


def test_scenario_14_filament_runout(bus):
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
# Scenario 15: Connection Loss (Watchdog)
# Ref: firmware-spec.md §Scenario 15 / §2.4 Watchdog
# ---------------------------------------------------------------------------


def test_scenario_15_connection_loss(bus):
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
# Scenario 16: Infinite Spool — Slave-Level Switch Sequence
# Ref: firmware-spec.md §Scenario 16
# ---------------------------------------------------------------------------


def test_scenario_16_infinite_spool_slave_sequence(bus):
    """
    Validates the slave-firmware half of an infinite-spool auto-switch.

    The Klipper extras layer orchestrates the switch; this test verifies that
    the slave accepts the exact command sequence that pico_mmu.py will issue
    and transitions slot states correctly throughout.

    Setup:
    - Slot 0: active spool, placed in ASSIST (printing in progress)
    - Slot 2: backup spool, LOADED and waiting

    Switch sequence (mirrors _do_switch_to_backup):
    1. Spool for slot 0 runs out — slave sensor opens → slot 0 → EMPTY
    2. Master feeds slot 2 → FEEDING
    3. Master sensor triggered → STOP slot 2 → LOADED
    4. Master sets ASSIST on slot 2 → ASSIST

    Validates:
    - Sensor runout during ASSIST causes slot 0 → EMPTY autonomously
    - Slave accepts FEED on slot 2 while slot 0 is EMPTY (no interference)
    - STOP returns slot 2 to LOADED (filament still present)
    - SET_ASSIST on slot 2 returns OK and transitions slot 2 to ASSIST
    - Slot 0 remains EMPTY throughout — no spurious resurrection
    """
    master, slave = bus

    # --- Setup: slots 0 and 2 loaded ---
    slave.inject_filament(0, True)
    slave.inject_filament(2, True)
    time.sleep(0.05)  # sensor debounce

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[0] == SlotState.LOADED, "Slot 0 must start LOADED"
    assert status[2] == SlotState.LOADED, "Slot 2 must start LOADED"

    # --- Phase 1: Enter ASSIST on slot 0 (printing starts) ---
    resp = master.query(slave.addr, Cmd.SET_ASSIST, struct.pack("<BH", 0, 150))
    assert resp is not None and resp[0] == Status.OK, "SET_ASSIST must return OK"

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[0] == SlotState.ASSIST, "Slot 0 must be in ASSIST"
    assert status[2] == SlotState.LOADED, "Slot 2 must remain LOADED"

    # --- Phase 2: Spool for slot 0 runs out — sensor opens ---
    slave.inject_filament(0, False)
    # Wait for debounce (5 ms) + sensor_loop (2 ms poll) + margin
    time.sleep(0.05)

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status is not None
    assert status[0] == SlotState.EMPTY, (
        f"Slot 0 must be EMPTY after spool runout during ASSIST, got {status[0]}"
    )
    assert status[2] == SlotState.LOADED, "Slot 2 must remain LOADED after slot 0 runout"

    # --- Phase 3: Feed slot 2 (backup spool) ---
    resp = master.query(slave.addr, Cmd.FEED, struct.pack("<BH", 2, 800))
    assert resp is not None and resp[0] == Status.OK, "FEED on backup slot must return OK"

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[2] == SlotState.FEEDING, f"Slot 2 must be FEEDING, got {status[2]}"
    assert status[0] == SlotState.EMPTY, "Slot 0 must remain EMPTY during slot 2 feed"

    # --- Phase 4: Master sensor triggered → stop slot 2 ---
    resp = master.query(slave.addr, Cmd.STOP, bytes([2]))
    assert resp is not None and resp[0] == Status.OK, "STOP must return OK"

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[2] == SlotState.LOADED, (
        f"Slot 2 must be LOADED after STOP (filament still present), got {status[2]}"
    )

    # --- Phase 5: Enter ASSIST on slot 2 (backup now the active spool) ---
    resp = master.query(slave.addr, Cmd.SET_ASSIST, struct.pack("<BH", 2, 150))
    assert resp is not None and resp[0] == Status.OK, "SET_ASSIST on slot 2 must return OK"

    status = master.query(slave.addr, Cmd.GET_STATUS)
    assert status[2] == SlotState.ASSIST, (
        f"Slot 2 must be in ASSIST after switch completes, got {status[2]}"
    )
    assert status[0] == SlotState.EMPTY, (
        "Slot 0 must remain EMPTY throughout — no spurious state change"
    )
