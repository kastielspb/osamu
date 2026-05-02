"""
PIO-based stepper motor pulse generator for RP2350.
Uses one PIO state machine per motor for jitter-free step generation.
"""

import rp2
from machine import Pin
from config import STEP_PINS, DIR_PINS, EN_PINS


# PIO program: generates step pulses at frequency controlled via TX FIFO.
# Each value written to FIFO = delay between steps in PIO cycles.
# Write 0 to stop. Minimum pulse width = 2 µs (step high time).
@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def step_pulse():
    """
    PIO step pulse generator.
    Pull delay value from FIFO → generate one step pulse → repeat.
    Blocking pull: stops when FIFO is empty (no auto-pull).
    """
    wrap_target()
    pull(block)          # Wait for new delay value
    mov(x, osr)          # X = delay count
    jmp(not_x, "stop")   # If X==0, stop (no pulses)

    label("pulse")
    set(pins, 1) [1]     # Step HIGH (min 2 cycles ≈ 2 µs at 1 MHz)
    set(pins, 0)          # Step LOW
    label("delay")
    jmp(x_dec, "delay")   # Delay loop
    jmp("pulse_done")

    label("stop")
    set(pins, 0)          # Ensure step LOW
    jmp("wrap_target")    # Wait for next FIFO value

    label("pulse_done")
    # After one step, check FIFO for new value (non-blocking)
    pull(noblock)
    mov(x, osr)           # X = new delay (or last value if FIFO empty)
    jmp(not_x, "stop")    # X==0 → stop
    jmp("pulse")
    wrap()


class Stepper:
    """
    Single stepper motor controlled via PIO step generation.

    Provides speed control (steps/sec) and direction.
    Enable/disable via the EN pin.
    """

    # PIO clock divider: run PIO at 1 MHz for easy timing math
    PIO_FREQ = 1_000_000

    def __init__(self, slot: int, pio_block: int = 0):
        """
        Args:
            slot: Slot index (0-3)
            pio_block: PIO block to use (0 or 1)
        """
        self._slot = slot
        self._step_pin = Pin(STEP_PINS[slot], Pin.OUT, value=0)
        self._dir_pin = Pin(DIR_PINS[slot], Pin.OUT, value=0)
        self._en_pin = Pin(EN_PINS[slot], Pin.OUT, value=1)  # Active LOW, start disabled

        self._sm = rp2.StateMachine(
            pio_block * 4 + slot,  # SM index: block0=[0,1,2,3], block1=[4,5,6,7]
            step_pulse,
            freq=self.PIO_FREQ,
            set_base=self._step_pin
        )
        self._running = False
        self._speed_hz = 0

    def enable(self):
        """Enable motor driver (EN pin active LOW)."""
        self._en_pin.value(0)

    def disable(self):
        """Disable motor driver."""
        self._en_pin.value(1)
        self._running = False

    def set_direction(self, forward: bool):
        """Set motor direction. True = forward (feed), False = reverse (retract)."""
        self._dir_pin.value(0 if forward else 1)

    def start(self, speed_hz: int, forward: bool = True):
        """
        Start step generation at given frequency.

        Args:
            speed_hz: Step frequency in Hz (steps per second)
            forward: Direction (True = feed, False = retract)
        """
        if speed_hz <= 0:
            self.stop()
            return

        self.set_direction(forward)
        self.enable()

        # Calculate PIO delay value: PIO runs at PIO_FREQ Hz
        # One step cycle = pulse_width + delay
        # delay_count = (PIO_FREQ / speed_hz) - pulse_overhead
        pulse_overhead = 4  # PIO instructions for pulse generation
        delay = max(1, (self.PIO_FREQ // speed_hz) - pulse_overhead)

        if not self._running:
            self._sm.active(1)
            self._running = True

        # Push delay value to PIO FIFO
        self._sm.put(delay)
        self._speed_hz = speed_hz

    def stop(self):
        """Stop step generation immediately."""
        if self._running:
            self._sm.put(0)  # Signal PIO to stop
            self._running = False
            self._speed_hz = 0

    def set_speed(self, speed_hz: int):
        """Change speed without changing direction (while running)."""
        if speed_hz <= 0:
            self.stop()
            return

        if self._running:
            pulse_overhead = 4
            delay = max(1, (self.PIO_FREQ // speed_hz) - pulse_overhead)
            self._sm.put(delay)
            self._speed_hz = speed_hz
        else:
            self.start(speed_hz)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def speed(self) -> int:
        return self._speed_hz


class StepperBank:
    """Manages all 4 stepper motors."""

    def __init__(self, pio_block: int = 0):
        self.motors = [Stepper(slot, pio_block) for slot in range(4)]

    def stop_all(self):
        """Emergency stop all motors."""
        for motor in self.motors:
            motor.stop()
            motor.disable()

    def __getitem__(self, slot: int) -> Stepper:
        return self.motors[slot]
