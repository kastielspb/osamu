"""
Microswitch sensor driver with software debounce.
"""

import time

from config import NUM_SLOTS, SENSOR_DEBOUNCE_MS, SENSOR_PINS
from machine import Pin


class Sensor:
    """
    Filament presence sensor (microswitch).
    Active LOW: switch closes (pulls to GND) when filament is present.
    """

    def __init__(self, slot: int):
        self._pin = Pin(SENSOR_PINS[slot], Pin.IN, Pin.PULL_UP)
        self._slot = slot
        self._state = False  # Debounced state
        self._last_raw = self._pin.value()
        self._last_change = time.ticks_ms()

    @property
    def is_triggered(self) -> bool:
        """True if filament is detected (debounced)."""
        return self._state

    @property
    def raw_value(self) -> int:
        """Raw pin value (0 = triggered/filament present, 1 = open/no filament)."""
        return self._pin.value()

    def update(self) -> bool:
        """
        Poll and debounce the sensor.
        Call this frequently (every 1-5 ms).

        Returns:
            True if state changed since last update.
        """
        raw = self._pin.value()
        now = time.ticks_ms()

        if raw != self._last_raw:
            self._last_raw = raw
            self._last_change = now
            return False

        # Stable for debounce period?
        elapsed = time.ticks_diff(now, self._last_change)
        if elapsed >= SENSOR_DEBOUNCE_MS:
            new_state = raw == 0  # Active LOW
            if new_state != self._state:
                self._state = new_state
                return True  # State changed

        return False


class SensorBank:
    """Manages all slot sensors."""

    def __init__(self):
        self.sensors = [Sensor(i) for i in range(NUM_SLOTS)]

    def update_all(self) -> list:
        """
        Update all sensors. Returns list of slot indices that changed state.
        """
        changed = []
        for i, sensor in enumerate(self.sensors):
            if sensor.update():
                changed.append(i)
        return changed

    def get_states(self) -> int:
        """Get all sensor states as a bitmask (bit0=slot0, etc.)."""
        mask = 0
        for i, sensor in enumerate(self.sensors):
            if sensor.is_triggered:
                mask |= 1 << i
        return mask

    def __getitem__(self, slot: int) -> Sensor:
        return self.sensors[slot]
