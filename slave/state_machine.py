"""
Main state machine and command dispatcher for the slave node.
Coordinates 4 slots, handles RS485 commands, and manages watchdog.
"""

import struct
import time
import urandom
import uasyncio as asyncio
from machine import unique_id

from bus.protocol import (
    CMD_PING, CMD_DISCOVER, CMD_ASSIGN_ADDR, CMD_GET_STATUS,
    CMD_FEED, CMD_RETRACT, CMD_SET_ASSIST, CMD_STOP, CMD_STOP_ALL,
    CMD_SET_LED, CMD_GET_CONFIG, CMD_SET_CURRENT, CMD_HOME_SLOT,
    ADDR_UNASSIGNED, ADDR_BROADCAST,
    STATUS_OK, STATUS_BUSY, STATUS_ERROR_INVALID_SLOT, STATUS_UNKNOWN_CMD,
    SLOT_EMPTY, SLOT_LOADED, Frame
)
from bus.rs485 import RS485
from motor.slot import Slot
from motor.stepper import StepperBank
from motor.tmc2209 import TMC2209Bank
from peripheral.sensor import SensorBank
from peripheral.led import LEDStrip
from config import (
    WATCHDOG_TIMEOUT_MS, STALLGUARD_POLL_MS,
    DEFAULT_RUN_CURRENT_MA, DEFAULT_HOLD_CURRENT_MA,
    DEVICE_ADDR_UNASSIGNED
)


# Firmware version (major.minor as 2 bytes)
FW_VERSION = (0, 1)


def _load_address() -> int:
    """Load saved address from flash NVS. Returns 0xFF if not set."""
    try:
        with open('addr.cfg', 'r') as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return DEVICE_ADDR_UNASSIGNED


def _save_address(addr: int):
    """Save address to flash NVS."""
    with open('addr.cfg', 'w') as f:
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
            Slot(i, self._steppers[i], self._tmc_bank.drivers[i], self._sensors[i])
            for i in range(4)
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
        if frame.addr not in (self._addr, ADDR_BROADCAST, ADDR_UNASSIGNED):
            return  # Not for us

        # Discovery: only respond if unassigned
        if frame.cmd == CMD_DISCOVER:
            if self._addr == DEVICE_ADDR_UNASSIGNED:
                # Random backoff to reduce collisions
                await asyncio.sleep_ms(urandom.getrandbits(6))  # 0-63 ms
                await self._rs485.send_response(
                    ADDR_UNASSIGNED, CMD_DISCOVER, frame.seq,
                    self._unique_id
                )
            return

        # Assign address: match by unique_id
        if frame.cmd == CMD_ASSIGN_ADDR:
            if len(frame.data) >= 9:
                uid = frame.data[:8]
                new_addr = frame.data[8]
                if uid == self._unique_id:
                    self._addr = new_addr
                    _save_address(new_addr)
                    await self._rs485.send_response(
                        new_addr, CMD_ASSIGN_ADDR, frame.seq,
                        bytes([STATUS_OK])
                    )
            return

        # All other commands require a valid assigned address
        if self._addr == DEVICE_ADDR_UNASSIGNED:
            return

        # Only process commands addressed to us (not broadcast for most cmds)
        if frame.addr != self._addr and frame.addr != ADDR_BROADCAST:
            return

        # Reset watchdog on any valid command
        self._last_poll_time = time.ticks_ms()
        self._watchdog_triggered = False

        # Dispatch
        response_data = self._dispatch_command(frame.cmd, frame.data)
        if response_data is not None and frame.addr != ADDR_BROADCAST:
            await self._rs485.send_response(
                self._addr, frame.cmd, frame.seq, response_data
            )

    def _dispatch_command(self, cmd: int, data: bytes) -> bytes | None:
        """
        Dispatch command and return response data (or None for no response).
        """
        if cmd == CMD_PING:
            return struct.pack('BB', FW_VERSION[0], FW_VERSION[1])

        elif cmd == CMD_GET_STATUS:
            states = bytes([s.state for s in self._slots])
            sensors = bytes([self._sensors.get_states()])
            errors = bytes([
                self._slots[0].error_code | (self._slots[1].error_code << 2) |
                (self._slots[2].error_code << 4) | (self._slots[3].error_code << 6)
            ])
            return states + sensors + errors

        elif cmd == CMD_FEED:
            if len(data) < 1:
                return bytes([STATUS_ERROR_INVALID_SLOT])
            slot = data[0]
            speed = struct.unpack('<H', data[1:3])[0] if len(data) >= 3 else 0
            if not (0 <= slot < 4):
                return bytes([STATUS_ERROR_INVALID_SLOT])
            status = self._slots[slot].feed(speed)
            if status == STATUS_OK:
                self._leds.set_feeding(slot)
            return bytes([status])

        elif cmd == CMD_RETRACT:
            if len(data) < 1:
                return bytes([STATUS_ERROR_INVALID_SLOT])
            slot = data[0]
            speed = struct.unpack('<H', data[1:3])[0] if len(data) >= 3 else 0
            if not (0 <= slot < 4):
                return bytes([STATUS_ERROR_INVALID_SLOT])
            status = self._slots[slot].retract(speed)
            if status == STATUS_OK:
                self._leds.set_feeding(slot)
            return bytes([status])

        elif cmd == CMD_SET_ASSIST:
            if len(data) < 1:
                return bytes([STATUS_ERROR_INVALID_SLOT])
            slot = data[0]
            current = struct.unpack('<H', data[1:3])[0] if len(data) >= 3 else 0
            if not (0 <= slot < 4):
                return bytes([STATUS_ERROR_INVALID_SLOT])
            status = self._slots[slot].set_assist(current)
            if status == STATUS_OK:
                self._leds.set_assist(slot)
            return bytes([status])

        elif cmd == CMD_STOP:
            if len(data) < 1:
                return bytes([STATUS_ERROR_INVALID_SLOT])
            slot = data[0]
            if not (0 <= slot < 4):
                return bytes([STATUS_ERROR_INVALID_SLOT])
            status = self._slots[slot].stop()
            self._update_slot_led(slot)
            return bytes([status])

        elif cmd == CMD_STOP_ALL:
            for s in self._slots:
                s.emergency_stop()
            self._steppers.stop_all()
            for i in range(4):
                self._update_slot_led(i)
            return bytes([STATUS_OK])

        elif cmd == CMD_SET_LED:
            if len(data) < 5:
                return bytes([STATUS_ERROR_INVALID_SLOT])
            mode, slot, r, g, b = data[0], data[1], data[2], data[3], data[4]
            if not (0 <= slot < 4):
                return bytes([STATUS_ERROR_INVALID_SLOT])
            self._leds.set_slot(slot, mode, r, g, b)
            return bytes([STATUS_OK])

        elif cmd == CMD_GET_CONFIG:
            # Return: unique_id(8) + addr(1) + fw_ver(2) + num_slots(1)
            config = self._unique_id + bytes([self._addr]) + \
                     struct.pack('BB', FW_VERSION[0], FW_VERSION[1]) + bytes([4])
            return config

        elif cmd == CMD_SET_CURRENT:
            if len(data) < 5:
                return bytes([STATUS_ERROR_INVALID_SLOT])
            slot = data[0]
            run_ma = struct.unpack('<H', data[1:3])[0]
            hold_ma = struct.unpack('<H', data[3:5])[0]
            if not (0 <= slot < 4):
                return bytes([STATUS_ERROR_INVALID_SLOT])
            self._tmc_bank.set_slot_current(slot, run_ma, hold_ma)
            return bytes([STATUS_OK])

        elif cmd == CMD_HOME_SLOT:
            if len(data) < 1:
                return bytes([STATUS_ERROR_INVALID_SLOT])
            slot = data[0]
            if not (0 <= slot < 4):
                return bytes([STATUS_ERROR_INVALID_SLOT])
            # Home = feed until sensor triggers
            status = self._slots[slot].feed()
            return bytes([status])

        else:
            return bytes([STATUS_UNKNOWN_CMD])

    def _update_slot_led(self, slot: int):
        """Update LED based on current slot state."""
        state = self._slots[slot].state
        if state == SLOT_EMPTY:
            self._leds.set_slot(slot, 0)  # OFF
        elif state == SLOT_LOADED:
            self._leds.set_loaded(slot)
        elif state in (SLOT_FEEDING, SLOT_RETRACTING):
            self._leds.set_feeding(slot)
        elif state == 0x04:  # ASSIST
            self._leds.set_assist(slot)
        elif state == 0x05:  # ERROR
            self._leds.set_error(slot)

    # --- Periodic tasks ---

    async def _sensor_loop(self):
        """Poll sensors at high frequency and handle state transitions."""
        while True:
            changed = self._sensors.update_all()
            for slot_id in changed:
                slot = self._slots[slot_id]
                sensor = self._sensors[slot_id]
                # Handle retract completion (sensor cleared)
                if not sensor.is_triggered and slot.state == 0x03:  # RETRACTING
                    slot.on_retract_complete()
                    self._update_slot_led(slot_id)
                # Handle idle state update
                elif slot.state in (SLOT_EMPTY, SLOT_LOADED):
                    slot.update_from_sensor()
                    self._update_slot_led(slot_id)
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
                for i in range(4):
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
                if slot.check_timeout():
                    self._update_slot_led(slot.id)
                if slot.check_stallguard():
                    self._leds.set_error(slot.id)
            await asyncio.sleep_ms(STALLGUARD_POLL_MS)
