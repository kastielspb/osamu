"""
Slot state machine for a single filament slot.
Manages motor, sensor, and state transitions.
"""

import time

from bus.protocol import SlotState, Status
from config import (
    DEFAULT_ASSIST_CURRENT_MA,
    DEFAULT_FEED_SPEED_HZ,
    DEFAULT_HOLD_CURRENT_MA,
    DEFAULT_RETRACT_SPEED_HZ,
    DEFAULT_RUN_CURRENT_MA,
    FEED_TIMEOUT_MS,
    RETRACT_TIMEOUT_MS,
)


class Slot:
    """
    State machine for a single filament slot.

    States:
        EMPTY      - No filament detected (sensor not triggered)
        LOADED     - Filament present, motor idle
        FEEDING    - Actively pushing filament toward extruder
        RETRACTING - Actively pulling filament back
        ASSIST     - Low-current friction compensation mode
        ERROR      - Jam/timeout detected, needs reset
    """

    def __init__(self, slot_id: int, stepper, tmc_driver, sensor):
        """
        Args:
            slot_id: Slot index (0-3)
            stepper: Stepper instance for this slot
            tmc_driver: TMC2209 instance for this slot
            sensor: Sensor instance for this slot
        """
        self.id = slot_id
        self._stepper = stepper
        self._tmc = tmc_driver
        self._sensor = sensor
        self._state = SlotState.EMPTY
        self._error_code = Status.OK
        self._operation_start = 0
        self._task = None

    @property
    def state(self) -> int:
        return self._state

    @property
    def error_code(self) -> int:
        return self._error_code

    @property
    def has_filament(self) -> bool:
        return self._sensor.is_triggered

    def _set_state(self, new_state: int):
        """Transition to a new state."""
        self._state = new_state
        if new_state != SlotState.ERROR:
            self._error_code = Status.OK

    def update_from_sensor(self):
        """
        Update state based on sensor reading (called periodically).
        Only transitions EMPTY↔LOADED when motor is idle.
        """
        if self._state == SlotState.EMPTY and self.has_filament:
            self._set_state(SlotState.LOADED)
        elif self._state == SlotState.LOADED and not self.has_filament:
            self._set_state(SlotState.EMPTY)

    def feed(self, speed_hz: int = 0) -> int:
        """
        Start feeding filament (push toward extruder).

        Returns:
            Status code (OK, BUSY, ERROR_SLOT_EMPTY)
        """
        if self._state in (SlotState.FEEDING, SlotState.RETRACTING, SlotState.ASSIST):
            return Status.BUSY

        if self._state == SlotState.EMPTY:
            return Status.ERROR_SLOT_EMPTY

        if self._state == SlotState.ERROR:
            return Status.BUSY

        speed = speed_hz if speed_hz > 0 else DEFAULT_FEED_SPEED_HZ
        self._stepper.start(speed, forward=True)
        self._set_state(SlotState.FEEDING)
        self._operation_start = time.ticks_ms()
        return Status.OK

    def retract(self, speed_hz: int = 0) -> int:
        """
        Start retracting filament (pull back).

        Returns:
            Status code
        """
        if self._state in (SlotState.FEEDING, SlotState.RETRACTING):
            return Status.BUSY

        if self._state == SlotState.EMPTY:
            return Status.ERROR_SLOT_EMPTY

        speed = speed_hz if speed_hz > 0 else DEFAULT_RETRACT_SPEED_HZ

        # If in assist, stop first then retract
        if self._state == SlotState.ASSIST:
            self._stepper.stop()

        self._stepper.start(speed, forward=False)
        self._set_state(SlotState.RETRACTING)
        self._operation_start = time.ticks_ms()
        return Status.OK

    def set_assist(self, current_ma: int = 0) -> int:
        """
        Enter assist mode (low current continuous feed).

        Returns:
            Status code
        """
        if self._state == SlotState.EMPTY:
            return Status.ERROR_SLOT_EMPTY

        if self._state == SlotState.ERROR:
            return Status.BUSY

        current = current_ma if current_ma > 0 else DEFAULT_ASSIST_CURRENT_MA
        self._tmc.set_run_current(current)
        self._stepper.start(DEFAULT_FEED_SPEED_HZ // 2, forward=True)
        self._set_state(SlotState.ASSIST)
        return Status.OK

    def stop(self) -> int:
        """Stop motor and return to idle state (LOADED or EMPTY based on sensor)."""
        self._stepper.stop()

        # Restore default current
        self._tmc.set_current(DEFAULT_RUN_CURRENT_MA, DEFAULT_HOLD_CURRENT_MA)

        if self.has_filament:
            self._set_state(SlotState.LOADED)
        else:
            self._set_state(SlotState.EMPTY)
        return Status.OK

    def emergency_stop(self):
        """Immediate stop without state consideration."""
        self._stepper.stop()
        self._stepper.disable()
        if self.has_filament:
            self._set_state(SlotState.LOADED)
        else:
            self._set_state(SlotState.EMPTY)

    def check_timeout(self) -> bool:
        """
        Check if current operation has timed out.
        Returns True if timeout occurred (state transitions to ERROR).
        """
        if self._state == SlotState.FEEDING:
            elapsed = time.ticks_diff(time.ticks_ms(), self._operation_start)
            if elapsed > FEED_TIMEOUT_MS:
                self._stepper.stop()
                self._set_state(SlotState.ERROR)
                self._error_code = Status.ERROR_TIMEOUT
                return True
        elif self._state == SlotState.RETRACTING:
            elapsed = time.ticks_diff(time.ticks_ms(), self._operation_start)
            if elapsed > RETRACT_TIMEOUT_MS:
                self._stepper.stop()
                self._set_state(SlotState.ERROR)
                self._error_code = Status.ERROR_TIMEOUT
                return True
        return False

    def check_stallguard(self) -> bool:
        """
        Check TMC2209 StallGuard for jam detection.
        Only active during FEEDING or RETRACTING.
        Returns True if jam detected.
        """
        if self._state not in (SlotState.FEEDING, SlotState.RETRACTING):
            return False

        if self._tmc.is_stalled():
            self._stepper.stop()
            self._set_state(SlotState.ERROR)
            self._error_code = Status.ERROR_JAM
            return True
        return False

    def on_retract_complete(self):
        """Called when sensor clears during retract (filament fully pulled back)."""
        if self._state == SlotState.RETRACTING:
            self._stepper.stop()
            self._set_state(SlotState.EMPTY)
