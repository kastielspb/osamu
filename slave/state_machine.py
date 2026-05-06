"""
Main state machine and command dispatcher for the slave node.
Coordinates filament slots, handles RS485 commands, and manages watchdog.
"""

import struct
import time

import uasyncio as asyncio
import urandom
from bus.protocol import Addr, Cmd, Frame, SlotState, Status
from bus.rs485 import RS485
from config import (
    DEFAULT_HOLD_CURRENT_MA,
    DEFAULT_RUN_CURRENT_MA,
    DEVICE_ADDR_UNASSIGNED,
    NUM_SLOTS,
    STALLGUARD_POLL_MS,
    WATCHDOG_TIMEOUT_MS,
)
from machine import unique_id
from motor.slot import Slot
from motor.stepper import StepperBank
from motor.tmc2209 import TMC2209Bank
from peripheral.led import LEDStrip
from peripheral.sensor import SensorBank

# Firmware version (major.minor as 2 bytes)
FW_VERSION = (0, 1)


def _load_address() -> int:
    """Load saved address from flash NVS. Returns 0xFF if not set."""
    try:
        with open("addr.cfg", "r") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return DEVICE_ADDR_UNASSIGNED


def _save_address(addr: int):
    """Save address to flash NVS."""
    with open("addr.cfg", "w") as f:
        f.write(str(addr))


class SlaveController:
    """
    Main controller for the slave node.
    Handles command dispatch, periodic tasks, and safety watchdog.
    """

    def __init__(self):
        self._addr = _load_address()
        self._unique_id = unique_id()  # 8 bytes RP2350 ROM ID

        # Hardware
        self._rs485 = RS485()
        self._steppers = StepperBank(pio_block=0)
        self._tmc_bank = TMC2209Bank()
        self._sensors = SensorBank()
        self._leds = LEDStrip()

        # Slots
        self._slots = [
            Slot(i, self._steppers[i], self._tmc_bank.drivers[i], self._sensors[i], self._leds)
            for i in range(NUM_SLOTS)
        ]

        # Watchdog
        self._last_poll_time = time.ticks_ms()
        self._watchdog_triggered = False

        # Sequence counter
        self._seq = 0

    @property
    def address(self) -> int:
        return self._addr

    def init_hardware(self):
        """Initialize all hardware (call from Core 1 for TMC, or before asyncio)."""
        self._tmc_bank.init_all(DEFAULT_RUN_CURRENT_MA, DEFAULT_HOLD_CURRENT_MA)
        self._leds.set_all_off()

    async def run(self):
        """Main asyncio loop: poll RS485, sensors, watchdog, LEDs."""
        asyncio.create_task(self._sensor_loop())
        asyncio.create_task(self._watchdog_loop())
        asyncio.create_task(self._led_loop())
        asyncio.create_task(self._stallguard_loop())

        # Main loop: RS485 command processing
        while True:
            frame = self._rs485.poll_rx()
            if frame is not None:
                await self._handle_frame(frame)
            await asyncio.sleep_ms(1)

    async def _handle_frame(self, frame: Frame):
        """Process an incoming RS485 frame."""
        # Address filtering
        if frame.addr not in (self._addr, Addr.BROADCAST, Addr.UNASSIGNED):
            return  # Not for us

        # Discovery: only respond if unassigned
        if frame.cmd == Cmd.DISCOVER:
            if self._addr == DEVICE_ADDR_UNASSIGNED:
                # Random backoff to reduce collisions
                await asyncio.sleep_ms(urandom.getrandbits(6))  # 0-63 ms
                await self._rs485.send_response(
                    Addr.UNASSIGNED, Cmd.DISCOVER, frame.seq, self._unique_id
                )
            return

        # Assign address: match by unique_id
        if frame.cmd == Cmd.ASSIGN_ADDR:
            if len(frame.data) >= 9:
                uid = frame.data[:8]
                new_addr = frame.data[8]
                if uid == self._unique_id:
                    self._addr = new_addr
                    _save_address(new_addr)
                    await self._rs485.send_response(
                        new_addr, Cmd.ASSIGN_ADDR, frame.seq, bytes([Status.OK])
                    )
            return

        # All other commands require a valid assigned address
        if self._addr == DEVICE_ADDR_UNASSIGNED:
            return

        # Only process commands addressed to us (not broadcast for most cmds)
        if frame.addr != self._addr and frame.addr != Addr.BROADCAST:
            return

        # Reset watchdog on any valid command
        self._last_poll_time = time.ticks_ms()
        self._watchdog_triggered = False

        # Dispatch
        if frame.cmd == Cmd.SET_ASSIST or frame.cmd == 0x0A:
            print(
                f"_handle_frame: got SET_ASSIST cmd={frame.cmd} addr={frame.addr} seq={frame.seq} data={frame.data.hex()}",
                flush=True,
            )
        response_data = self._dispatch_command(frame.cmd, frame.data)
        if response_data is not None and frame.addr != Addr.BROADCAST:
            await self._rs485.send_response(self._addr, frame.cmd, frame.seq, response_data)

    def _dispatch_command(self, cmd: int, data: bytes) -> bytes | None:
        """
        Dispatch command and return response data (or None for no response).
        """
        if cmd == Cmd.PING:
            return struct.pack("BB", FW_VERSION[0], FW_VERSION[1])

        elif cmd == Cmd.GET_STATUS:
            states = bytes([s.state for s in self._slots])
            sensors = bytes([self._sensors.get_states()])
            errors_val = 0
            for _i, _s in enumerate(self._slots):
                errors_val |= _s.error_code << (_i * 2)
            errors = bytes([errors_val])
            if states[0] != 0 and states[0] != 1:
                print(f"GET_STATUS: slot0_state={states[0]} (unexpected!)", flush=True)
            return states + sensors + errors

        elif cmd == Cmd.FEED:
            if len(data) < 1:
                return bytes([Status.ERROR_INVALID_SLOT])

            slot = data[0]
            speed = struct.unpack("<H", data[1:3])[0] if len(data) >= 3 else 0
            if not (0 <= slot < NUM_SLOTS):
                return bytes([Status.ERROR_INVALID_SLOT])

            status = self._slots[slot].feed(speed)
            if status != Status.OK:
                print(
                    f"FEED slot={slot} state={self._slots[slot].state} → status={status}",
                    flush=True,
                )
            return bytes([status])

        elif cmd == Cmd.RETRACT:
            if len(data) < 1:
                return bytes([Status.ERROR_INVALID_SLOT])

            slot = data[0]
            speed = struct.unpack("<H", data[1:3])[0] if len(data) >= 3 else 0
            if not (0 <= slot < NUM_SLOTS):
                return bytes([Status.ERROR_INVALID_SLOT])

            status = self._slots[slot].retract(speed)
            return bytes([status])

        elif cmd == Cmd.SET_ASSIST:
            print(f"SET_ASSIST dispatch: cmd={cmd} data={data.hex()}", flush=True)
            if len(data) < 1:
                return bytes([Status.ERROR_INVALID_SLOT])

            slot = data[0]
            current = struct.unpack("<H", data[1:3])[0] if len(data) >= 3 else 0
            if not (0 <= slot < NUM_SLOTS):
                return bytes([Status.ERROR_INVALID_SLOT])

            status = self._slots[slot].set_assist(current)
            return bytes([status])

        elif cmd == Cmd.STOP:
            if len(data) < 1:
                return bytes([Status.ERROR_INVALID_SLOT])

            slot = data[0]
            if not (0 <= slot < NUM_SLOTS):
                return bytes([Status.ERROR_INVALID_SLOT])

            status = self._slots[slot].stop()
            return bytes([status])

        elif cmd == Cmd.STOP_ALL:
            for s in self._slots:
                s.emergency_stop()
            self._steppers.stop_all()
            return bytes([Status.OK])

        elif cmd == Cmd.GET_CONFIG:
            # Return: unique_id(8) + addr(1) + fw_ver(2) + num_slots(1)
            config = (
                self._unique_id
                + bytes([self._addr])
                + struct.pack("BB", FW_VERSION[0], FW_VERSION[1])
                + bytes([NUM_SLOTS])
            )
            return config

        elif cmd == Cmd.SET_CURRENT:
            if len(data) < 5:
                return bytes([Status.ERROR_INVALID_SLOT])

            slot = data[0]
            run_ma = struct.unpack("<H", data[1:3])[0]
            hold_ma = struct.unpack("<H", data[3:5])[0]
            if not (0 <= slot < NUM_SLOTS):
                return bytes([Status.ERROR_INVALID_SLOT])

            self._tmc_bank.set_slot_current(slot, run_ma, hold_ma)
            return bytes([Status.OK])

        elif cmd == Cmd.HOME_SLOT:
            if len(data) < 1:
                return bytes([Status.ERROR_INVALID_SLOT])

            slot = data[0]
            if not (0 <= slot < NUM_SLOTS):
                return bytes([Status.ERROR_INVALID_SLOT])

            # Home = feed until sensor triggers
            status = self._slots[slot].feed()
            return bytes([status])

        elif cmd == Cmd.SET_FILAMENT:
            if len(data) < 8:
                return bytes([Status.ERROR_INVALID_SLOT])

            slot, r, g, b = data[0], data[1], data[2], data[3]
            material = data[4:8].rstrip(b"\x00")
            if not (0 <= slot < NUM_SLOTS):
                return bytes([Status.ERROR_INVALID_SLOT])

            self._slots[slot].set_filament(r, g, b, material)
            return bytes([Status.OK])

        else:
            return bytes([Status.UNKNOWN_CMD])

    # --- Periodic tasks ---

    async def _sensor_loop(self):
        """Poll sensors at high frequency and handle state transitions."""
        while True:
            changed = self._sensors.update_all()
            for slot_id in changed:
                slot = self._slots[slot_id]
                sensor = self._sensors[slot_id]
                # Handle retract completion (sensor cleared)
                if not sensor.is_triggered and slot.state == SlotState.RETRACTING:
                    slot.on_retract_complete()
                # Handle filament runout during assist mode
                elif not sensor.is_triggered and slot.state == SlotState.ASSIST:
                    slot.stop()
                # Handle idle state update
                elif slot.state in (SlotState.EMPTY, SlotState.LOADED):
                    slot.update_from_sensor()
            await asyncio.sleep_ms(2)

    async def _watchdog_loop(self):
        """Safety watchdog: stop motors if master goes silent."""
        while True:
            elapsed = time.ticks_diff(time.ticks_ms(), self._last_poll_time)
            if elapsed > WATCHDOG_TIMEOUT_MS and not self._watchdog_triggered:
                # Master timeout — emergency stop
                self._watchdog_triggered = True
                for s in self._slots:
                    s.emergency_stop()
                self._steppers.stop_all()
                self._leds.set_all_off()
                # Blink all red to indicate watchdog
                for i in range(NUM_SLOTS):
                    self._leds.set_error(i)
            await asyncio.sleep_ms(500)

    async def _led_loop(self):
        """Update LED animations at ~30 Hz."""
        while True:
            self._leds.update()
            await asyncio.sleep_ms(33)

    async def _stallguard_loop(self):
        """Check StallGuard for jam detection during active operations."""
        while True:
            for slot in self._slots:
                slot.check_timeout()
                slot.check_stallguard()
            await asyncio.sleep_ms(STALLGUARD_POLL_MS)
