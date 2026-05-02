"""
SK6812 RGBW / WS2812B LED strip driver using PIO on RP2350.
4 LEDs per slot (16 total), individual slot color/mode control.
Supports both SK6812 (RGBW, 32-bit) and WS2812B (RGB, 24-bit).
"""

import array
import time

import rp2
from bus.protocol import LedMode
from config import LED_COUNT, LED_PIN
from machine import Pin

# Set to True for SK6812 RGBW, False for WS2812B RGB
RGBW_MODE = True


@rp2.asm_pio(
    sideset_init=rp2.PIO.OUT_LOW, out_shiftdir=rp2.PIO.SHIFT_LEFT, autopull=True, pull_thresh=32
)
def sk6812():
    """PIO program for SK6812/WS2812B protocol (800 kHz, 32-bit per pixel)."""
    # Timing identical to WS2812B — SK6812 is protocol-compatible
    # T0H=0.3µs, T1H=0.6µs, T0L=0.9µs, T1L=0.6µs (period=1.25µs)
    wrap_target()
    label("bitloop")
    out(x, 1).side(0)[2]
    jmp(not_x, "do_zero").side(1)[1]
    jmp("bitloop").side(1)[4]
    label("do_zero")
    nop().side(0)[4]
    wrap()


class LEDStrip:
    """
    SK6812 RGBW / WS2812B LED strip controller.
    16 LEDs total, 4 per slot. Supports solid, breathe, and blink modes.
    """

    LEDS_PER_SLOT = 4

    def __init__(self, rgbw: bool = RGBW_MODE):
        self._rgbw = rgbw
        self._sm = rp2.StateMachine(
            4,  # Use PIO block 1, SM 0 (index 4)
            sk6812,
            freq=8_000_000,  # 8 MHz for proper SK6812/WS2812B timing
            sideset_base=Pin(LED_PIN),
        )
        self._sm.active(1)

        self._buf = array.array("I", [0] * LED_COUNT)
        self._modes = [LedMode.OFF] * 4  # Per-slot mode
        self._colors = [(0, 0, 0, 0)] * 4  # Per-slot RGBW
        self._tick = 0

    def _pack_color(self, r: int, g: int, b: int, w: int = 0) -> int:
        """Pack color into LED format.
        SK6812 RGBW: GRBW (32-bit)
        WS2812B RGB: GRB0 (upper 24 bits of 32-bit word)
        """
        if self._rgbw:
            return (g << 24) | (r << 16) | (b << 8) | w
        else:
            return (g << 24) | (r << 16) | (b << 8)

    def _write(self):
        """Push buffer to LED strip."""
        for i in range(LED_COUNT):
            self._sm.put(self._buf[i], 8)
        time.sleep_us(60)  # Reset pulse

    def set_slot(self, slot: int, mode: int, r: int = 0, g: int = 0, b: int = 0, w: int = 0):
        """
        Set LED mode and color for a slot.

        Args:
            slot: Slot index (0-3)
            mode: LED_MODE_OFF, LED_MODE_SOLID, LED_MODE_BREATHE, LED_MODE_BLINK
            r, g, b: Color values (0-255)
            w: White channel (0-255, SK6812 RGBW only)
        """
        if not (0 <= slot < 4):
            return

        self._modes[slot] = mode
        self._colors[slot] = (r, g, b, w)

    def set_all_off(self):
        """Turn off all LEDs."""
        for i in range(4):
            self._modes[i] = LedMode.OFF
        self._buf = array.array("I", [0] * LED_COUNT)
        self._write()

    def update(self):
        """
        Update LED animation frame. Call this at ~30-50 Hz.
        """
        self._tick += 1

        for slot in range(4):
            mode = self._modes[slot]
            r, g, b, w = self._colors[slot]
            start = slot * self.LEDS_PER_SLOT

            if mode == LedMode.OFF:
                color = 0
            elif mode == LedMode.SOLID:
                color = self._pack_color(r, g, b, w)
            elif mode == LedMode.BREATHE:
                # Sine-like breathing using triangle wave
                phase = (self._tick * 4) % 512
                if phase > 255:
                    brightness = 511 - phase
                else:
                    brightness = phase
                br = brightness / 255.0
                color = self._pack_color(int(r * br), int(g * br), int(b * br), int(w * br))
            elif mode == LedMode.BLINK:
                # 1 Hz blink (on 500ms, off 500ms at 50Hz update)
                on = (self._tick % 50) < 25
                color = self._pack_color(r, g, b, w) if on else 0
            else:
                color = 0

            for i in range(self.LEDS_PER_SLOT):
                self._buf[start + i] = color

        self._write()

    def set_error(self, slot: int):
        """Quick helper: set slot to blinking red (error indication)."""
        self.set_slot(slot, LedMode.BLINK, 255, 0, 0)

    def set_feeding(self, slot: int):
        """Quick helper: set slot to breathing blue (feeding)."""
        self.set_slot(slot, LedMode.BREATHE, 0, 0, 255)

    def set_loaded(self, slot: int):
        """Quick helper: set slot to solid green (loaded/ready)."""
        self.set_slot(slot, LedMode.SOLID, 0, 255, 0)

    def set_assist(self, slot: int):
        """Quick helper: set slot to solid cyan (assist mode)."""
        self.set_slot(slot, LedMode.SOLID, 0, 200, 200)
