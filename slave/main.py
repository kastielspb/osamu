"""
Pico-MMU Slave Firmware - Entry Point
Raspberry Pi Pico 2 (RP2350) - MicroPython

Initializes hardware and starts the asyncio event loop.
Core 0: RS485 + state machine + sensors
Core 1: TMC2209 initialization (then idle, PIO handles steps)
"""

import _thread
import time

import uasyncio as asyncio
from state_machine import SlaveController


def core1_entry():
    """
    Core 1 thread: Initialize TMC2209 drivers.
    After initialization, Core 1 is idle (PIO handles step generation).
    """
    # Import here to avoid circular imports during boot
    controller = _shared_controller
    controller.init_hardware()


# Global reference for cross-core access
_shared_controller = None


def main():
    global _shared_controller

    # Create the main controller
    _shared_controller = SlaveController()

    # Start Core 1 for hardware init
    _thread.start_new_thread(core1_entry, ())

    # Give Core 1 time to initialize TMC drivers
    time.sleep_ms(500)

    # Start the asyncio event loop on Core 0
    asyncio.run(_shared_controller.run())


if __name__ == "__main__":
    main()
