"""
TMC2209 UART driver for RP2350.
Single-wire UART interface with register read/write.
Supports setting run/hold current and reading StallGuard result.
"""

import struct
import time
from machine import UART, Pin
from config import (
    TMC_UART_ID, TMC_UART_TX_PIN, TMC_UART_RX_PIN,
    TMC_ADDRESSES, DEFAULT_RUN_CURRENT_MA, DEFAULT_HOLD_CURRENT_MA,
    STALLGUARD_THRESHOLD
)


# --- TMC2209 Register Addresses ---
REG_GCONF = 0x00
REG_GSTAT = 0x01
REG_IFCNT = 0x02
REG_IHOLD_IRUN = 0x10
REG_TPOWERDOWN = 0x11
REG_TSTEP = 0x12
REG_TCOOLTHRS = 0x14
REG_SGTHRS = 0x40
REG_SG_RESULT = 0x41
REG_COOLCONF = 0x42
REG_CHOPCONF = 0x6C
REG_DRV_STATUS = 0x6F

# GCONF bits
GCONF_EN_SPREADCYCLE = 0x00000004
GCONF_PDN_DISABLE = 0x00000040
GCONF_MSTEP_REG_SELECT = 0x00000080
GCONF_MULTISTEP_FILT = 0x00000100

# CHOPCONF defaults: TOFF=3, HSTRT=5, HEND=0, TBL=2, MRES=256 microsteps
CHOPCONF_DEFAULT = 0x10000053

# Current sense resistor (Ohm) — typical for TMC2209 modules
RSENSE = 0.11
VSENSE_LOW = 0.325  # V_FS at vsense=1


def _current_to_cs(current_ma: int) -> int:
    """Convert current in mA to CS register value (0-31)."""
    cs = int((current_ma / 1000.0 * 32.0 * 1.41421 * RSENSE / VSENSE_LOW) - 1)
    return max(0, min(31, cs))


def _crc8(data: bytes) -> int:
    """TMC2209 CRC8 calculation."""
    crc = 0
    for byte in data:
        for _ in range(8):
            if (crc >> 7) ^ (byte & 0x01):
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
            byte >>= 1
    return crc


class TMC2209:
    """
    TMC2209 stepper driver interface over single-wire UART.

    Each driver on the shared UART has a unique address (0-3) set by MS1/MS2 pins.
    """

    def __init__(self, uart: UART, address: int):
        self._uart = uart
        self._addr = address & 0x03
        self._ifcnt = 0

    def _write_reg(self, reg: int, value: int):
        """Write a 32-bit value to a TMC2209 register."""
        # Datagram: SYNC + SLAVE_ADDR + REG|0x80 + DATA[31:0] + CRC
        sync = 0x05
        addr = self._addr
        reg_w = reg | 0x80  # Write flag

        data = struct.pack('>I', value)
        datagram = bytes([sync, addr, reg_w]) + data
        crc = _crc8(datagram)
        datagram += bytes([crc])

        self._uart.write(datagram)
        time.sleep_us(500)  # Inter-frame delay
        # Read back echo (single-wire, we see our own TX)
        self._uart.read(len(datagram))

    def _read_reg(self, reg: int) -> int | None:
        """Read a 32-bit value from a TMC2209 register."""
        # Send read request: SYNC + SLAVE_ADDR + REG + CRC
        sync = 0x05
        addr = self._addr

        req = bytes([sync, addr, reg])
        crc = _crc8(req)
        req += bytes([crc])

        # Flush RX buffer
        self._uart.read()

        self._uart.write(req)
        time.sleep_us(500)

        # Read echo of our request (4 bytes) + response (8 bytes)
        echo = self._uart.read(4)  # Our TX echo
        time.sleep_ms(2)

        # Response: SYNC + 0xFF + REG + DATA[31:0] + CRC
        resp = self._uart.read(8)
        if resp is None or len(resp) < 8:
            return None

        # Verify CRC
        if _crc8(resp[:7]) != resp[7]:
            return None

        value = struct.unpack('>I', resp[3:7])[0]
        return value

    def init(self, run_current_ma: int = DEFAULT_RUN_CURRENT_MA,
             hold_current_ma: int = DEFAULT_HOLD_CURRENT_MA):
        """Initialize TMC2209 with default settings."""
        # GCONF: PDN_DISABLE (use UART), MSTEP_REG_SELECT, MULTISTEP_FILT
        gconf = GCONF_PDN_DISABLE | GCONF_MSTEP_REG_SELECT | GCONF_MULTISTEP_FILT
        self._write_reg(REG_GCONF, gconf)

        # CHOPCONF: default settings
        self._write_reg(REG_CHOPCONF, CHOPCONF_DEFAULT)

        # Set currents
        self.set_current(run_current_ma, hold_current_ma)

        # Set StallGuard threshold
        self._write_reg(REG_SGTHRS, STALLGUARD_THRESHOLD)

        # TCOOLTHRS: enable StallGuard above this speed
        self._write_reg(REG_TCOOLTHRS, 0xFFFFF)

        # TPOWERDOWN: delay before hold current kicks in (typ. 128)
        self._write_reg(REG_TPOWERDOWN, 128)

    def set_current(self, run_ma: int, hold_ma: int):
        """Set run and hold current in milliamps."""
        irun = _current_to_cs(run_ma)
        ihold = _current_to_cs(hold_ma)
        # IHOLD_IRUN register: IHOLD[4:0] | IRUN[12:8] | IHOLDDELAY[19:16]
        value = (ihold & 0x1F) | ((irun & 0x1F) << 8) | (6 << 16)
        self._write_reg(REG_IHOLD_IRUN, value)

    def set_run_current(self, current_ma: int):
        """Set only run current (read-modify-write)."""
        irun = _current_to_cs(current_ma)
        current = self._read_reg(REG_IHOLD_IRUN)
        if current is None:
            current = 0
        current = (current & ~(0x1F << 8)) | ((irun & 0x1F) << 8)
        self._write_reg(REG_IHOLD_IRUN, current)

    def read_stallguard(self) -> int | None:
        """Read StallGuard result (0-510). Lower = more load."""
        return self._read_reg(REG_SG_RESULT)

    def read_drv_status(self) -> int | None:
        """Read DRV_STATUS register (OT, stall flags, CS actual, etc.)."""
        return self._read_reg(REG_DRV_STATUS)

    def is_stalled(self) -> bool:
        """Check if motor is stalled based on StallGuard."""
        sg = self.read_stallguard()
        if sg is None:
            return False  # Can't read = assume OK (avoid false positives)
        return sg < STALLGUARD_THRESHOLD


class TMC2209Bank:
    """Manages all 4 TMC2209 drivers on a shared UART bus."""

    def __init__(self):
        self._uart = UART(
            TMC_UART_ID,
            baudrate=115200,
            tx=Pin(TMC_UART_TX_PIN),
            rx=Pin(TMC_UART_RX_PIN),
            bits=8, parity=None, stop=1
        )
        self.drivers = [TMC2209(self._uart, addr) for addr in TMC_ADDRESSES]

    def init_all(self, run_current_ma: int = DEFAULT_RUN_CURRENT_MA,
                 hold_current_ma: int = DEFAULT_HOLD_CURRENT_MA):
        """Initialize all 4 TMC2209 drivers."""
        for drv in self.drivers:
            drv.init(run_current_ma, hold_current_ma)
            time.sleep_ms(10)

    def set_slot_current(self, slot: int, run_ma: int, hold_ma: int):
        """Set current for a specific slot's driver."""
        if 0 <= slot < 4:
            self.drivers[slot].set_current(run_ma, hold_ma)

    def set_assist_current(self, slot: int, current_ma: int):
        """Set low assist current for friction compensation."""
        if 0 <= slot < 4:
            self.drivers[slot].set_run_current(current_ma)
