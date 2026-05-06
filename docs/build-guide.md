# Build Guide

## Contents

1. [Required Components](#1-required-components)
2. [Slave Box Assembly](#2-slave-box-assembly)
3. [Master Node Assembly](#3-master-node-assembly)
4. [Interconnect Bus (RJ45)](#4-interconnect-bus-rj45)
5. [Slave Firmware (MicroPython)](#5-slave-firmware-micropython)
6. [Master Firmware (Klipper MCU)](#6-master-firmware-klipper-mcu)
7. [Installing the Klipper Extras Module](#7-installing-the-klipper-extras-module)
8. [printer.cfg Configuration](#8-printercfg-configuration)
9. [First Boot and Verification](#9-first-boot-and-verification)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Required Components

### Per Slave Box (4 slots)

| Component | Qty | Notes |
|---|---|---|
| Raspberry Pi Pico 2 (RP2350) | 1 | Or Pico 2 W if Wi-Fi debug is needed |
| TMC2209 v3.1 (StepStick module) | 4 | Version with UART pins (PDN/DIAG) |
| Nema 14 stepper motor | 4 | 35 mm, 200 steps/rev, ~0.8 A. Alternative: Nema 17 pancake |
| MAX485 / SP485 module | 1 | TTL ↔ RS485 transceiver |
| Microswitch (with roller) | 4 | NO/NC, for filament detection |
| WS2812B LED strip | 4 | Cut exactly 4 LEDs (1 per slot) |
| DC-DC step-down (24 V → 5 V) | 1 | Min 3 A (for Pico + LEDs). MP1584 or LM2596 |
| RJ45 female jack (panel mount) | 2 | IN and OUT |
| USB-C panel-mount extension | 1 | For firmware updates without disassembly |
| PC4-M6 pneumatic fitting | 4+1 | 4 slot inputs + 1 splitter output |
| 4-to-1 splitter (printed) | 1 | Nylon / SLA, channel angles ≤20° |
| PTFE tube 2×4 mm | ~2 m | From spools to splitter |
| 100 µF 35 V electrolytic capacitor | 4 | One per TMC2209 (VMOT decoupling) |
| 100 nF ceramic capacitor | 4 | Next to TMC2209 (logic decoupling) |
| 1 kΩ resistor | 1 | TMC UART pull-up (between TX and RX) |

### Per Master Node

| Component | Qty | Notes |
|---|---|---|
| Raspberry Pi Pico 2 (RP2350) | 1 | |
| MAX485 / SP485 module | 1 | |
| Microswitch | 1 | Final sensor on splitter output |
| 4-to-1 splitter (printed) | 1 | Merges tubes from slave boxes |
| RJ45 female jack | 1 | Output to first slave |
| USB-C cable | 1 | To Klipper host (Raspberry Pi) |
| PC4-M6 pneumatic fitting | 4+1 | Inputs + output to extruder |

### Shared / General

| Component | Qty | Notes |
|---|---|---|
| 24 V PSU ≥10 A | 1 | Or tap from printer PSU |
| Cat5e Ethernet patch cable | One per slave | Standard straight-through (T568B–T568B) |
| PTFE tube 2×4 mm | ~1 m per slave | From slave box to Master |

---

## 2. Slave Box Assembly

### 2.1 Pico 2 Wiring (Slave)

```
                    ┌─────────────────────────────────┐
                    │       Raspberry Pi Pico 2       │
                    │            (RP2350)             │
                    │                                 │
          RS485     │  GP0 (UART0 TX) ──→ MAX485 DI   │
        transceiver │  GP1 (UART0 RX) ←── MAX485 RO   │
                    │  GP2 (GPIO OUT) ──→ MAX485 DE+RE│
                    │                                 │
          TMC UART  │  GP4 (UART1 TX) ──→ TMC PDN     │──┐ 1 kΩ
                    │  GP5 (UART1 RX) ←───────────────│──┘ (between TX/RX)
                    │                                 │
         Stepper 0  │  GP6  ──→ TMC0 STEP             │
                    │  GP7  ──→ TMC0 DIR              │
                    │  GP14 ──→ TMC0 EN               │
                    │                                 │
         Stepper 1  │  GP8  ──→ TMC1 STEP             │
                    │  GP9  ──→ TMC1 DIR              │
                    │  GP15 ──→ TMC1 EN               │
                    │                                 │
         Stepper 2  │  GP10 ──→ TMC2 STEP             │
                    │  GP11 ──→ TMC2 DIR              │
                    │  GP16 ──→ TMC2 EN               │
                    │                                 │
         Stepper 3  │  GP12 ──→ TMC3 STEP             │
                    │  GP13 ──→ TMC3 DIR              │
                    │  GP17 ──→ TMC3 EN               │
                    │                                 │
         Sensors    │  GP18 ←── Microswitch 0 (+ GND) │
                    │  GP19 ←── Microswitch 1 (+ GND) │
                    │  GP20 ←── Microswitch 2 (+ GND) │
                    │  GP21 ←── Microswitch 3 (+ GND) │
                    │                                 │
         LED        │  GP22 ──→ WS2812B DIN           │
                    │                                 │
         Power      │  VSYS ←── 5 V (from DC-DC)      │
                    │  GND  ←── GND                   │
                    └─────────────────────────────────┘
```

### 2.2 MAX485 Wiring (RS485 Transceiver)

```
MAX485 module         Pico 2            RJ45 Bus
─────────────         ──────            ────────
VCC ───────────────── 3V3
GND ───────────────── GND ───────────── Pin 3, 6 (GND)
DI  ───────────────── GP0 (TX)
RO  ───────────────── GP1 (RX)
DE  ───────────────── GP2
RE  ───────────────── GP2 (DE and RE tied together!)
A   ────────────────────────────────── Pin 4 (RS485 A)
B   ────────────────────────────────── Pin 5 (RS485 B)
```

> **Important:** DE and RE pins on the MAX485 are connected together and driven by a single GPIO. HIGH = transmit, LOW = receive.

### 2.3 TMC2209 Wiring (StepStick Modules)

All four TMC2209 modules wire the same way; only STEP/DIR/EN pins and the MS1/MS2 address differ.

```
TMC2209 StepStick      Connection
─────────────────      ──────────
VMOT ──────────────── +24 V (from RJ45 bus, pins 1, 2)
GND  ──────────────── GND
VIO  ──────────────── 3V3 (from Pico)
STEP ──────────────── GP6 / GP8 / GP10 / GP12 (by slot)
DIR  ──────────────── GP7 / GP9 / GP11 / GP13 (by slot)
EN   ──────────────── GP14 / GP15 / GP16 / GP17 (by slot)
PDN/UART ──────────── GP4 (shared UART TX, via 1 kΩ)
         ──────────── GP5 (UART RX, same 1 kΩ)

MS1, MS2 set UART address:
  TMC0: MS1=GND,  MS2=GND   → addr 0
  TMC1: MS1=3V3,  MS2=GND   → addr 1
  TMC2: MS1=GND,  MS2=3V3   → addr 2
  TMC3: MS1=3V3,  MS2=3V3   → addr 3

DIAG — leave unconnected (or to a spare GPIO for future hardware StallGuard IRQ)
```

**TMC UART single-wire shared bus:**

```
GP4 (TX) ───[1 kΩ]────┬──── TMC0 PDN
                      ├──── TMC1 PDN
                      ├──── TMC2 PDN
                      └──── TMC3 PDN
                      │
GP5 (RX) ─────────────┘
```

> 1 kΩ resistor between TX and the PDN bus. All 4 TMC drivers share one line — addressing via MS1/MS2.

### 2.4 Microswitch Wiring

```
Microswitch (NO)    Connection
────────────────    ──────────
C (Common)       →  GND
NO (Normally Open) →  GP18 / GP19 / GP20 / GP21

Internal PULL_UP enabled in firmware (Pin.PULL_UP).
When pressed (filament pushes lever): contact closes → GPIO = LOW → filament detected.
```

**Placement:** One microswitch at the input of each splitter channel, before the merge point. Filament depresses the lever as it passes through.

### 2.5 WS2812B Wiring

```
WS2812B strip     Connection
─────────────     ──────────
VCC (5 V)  ────── 5 V (from DC-DC)
GND        ────── GND
DIN        ────── GP22
```

> 1 LED per slot. Cut exactly NUM_SLOTS LEDs (4 for the default 4-slot configuration).

### 2.6 Slave Box Power

```
RJ45 pins 1, 2 (+24 V) ──→ VMOT of all TMC2209 (with 100 µF cap per driver)
                       ──→ DC-DC input (24 V → 5 V)

DC-DC 5 V output       ──→ Pico VSYS
                       ──→ WS2812B VCC

RJ45 pins 3, 6 (GND)   ──→ Common ground for the whole box
```

> **Capacitors:** 100 µF electrolytic + 100 nF ceramic next to each TMC2209 between VMOT and GND. These are mandatory — without them drivers will overheat or reset.

### 2.7 RJ45 Connectors (Daisy-Chain)

Both RJ45 jacks are wired in parallel (all matching pins bridged):

```
RJ45 IN (from master / previous slave)     RJ45 OUT (to next slave)
Pin 1 ────────────────────────────────────── Pin 1  (+24 V)
Pin 2 ────────────────────────────────────── Pin 2  (+24 V)
Pin 3 ────────────────────────────────────── Pin 3  (+24 V)
Pin 4 ────────────────────────────────────── Pin 4  (RS485 A)
Pin 5 ────────────────────────────────────── Pin 5  (RS485 B)
Pin 6 ────────────────────────────────────── Pin 6  (GND)
Pin 7 ────────────────────────────────────── Pin 7  (GND)
Pin 8 ────────────────────────────────────── Pin 8  (GND)
```

---

## 3. Master Node Assembly

### 3.1 Pico 2 Wiring (Master)

```
                    ┌─────────────────────────────────┐
                    │       Raspberry Pi Pico 2       │
                    │        (Master, RP2350)         │
                    │                                 │
          RS485     │  GP0 (UART0 TX) ──→ MAX485 DI   │
        transceiver │  GP1 (UART0 RX) ←── MAX485 RO   │
                    │  GP2 (GPIO OUT) ──→ MAX485 DE+RE│
                    │                                 │
          Sensor    │  GP3 (GPIO IN)  ←── Microswitch │
                    │                     (+ GND)     │
                    │                                 │
          USB-C     │  USB ────────────→ Klipper Host │
                    │                                 │
          Power     │  VBUS ←── 5 V (from USB host)   │
                    │  GND  ←── GND                   │
                    └─────────────────────────────────┘
```

The Master is considerably simpler than a Slave — it is only an RS485 bridge and a single sensor.

### 3.2 Final Microswitch

Mounted on the output of the Master splitter, before the PTFE tube that runs to the printer extruder.

```
Microswitch ──→ GP3 (PULL_UP enabled in firmware, active LOW)
```

When filament is fed from any slave, it reaches the Master splitter and depresses the switch → Klipper receives a "filament present" event.

---

## 4. Interconnect Bus (RJ45)

### 4.1 Pinout (T568B)

| Pin | Wire (T568B) | Purpose |
|---|---|---|
| 1 | Orange/white | +24 V |
| 2 | Orange | +24 V |
| 3 | Green/white | GND |
| 4 | Blue | RS485 A (+) |
| 5 | Blue/white | RS485 B (−) |
| 6 | Green | GND |
| 7 | Brown/white | +24 V (droop compensation) |
| 8 | Brown | GND (droop compensation) |

### 4.2 Cables

Use **standard straight-through Cat5e patch cables**. Segments up to 5 m work reliably at 115200 baud.

> **Do not use crossover cables!** Straight-through only (T568B on both ends).

### 4.3 RS485 Termination

On the last device in the chain (last slave, OUT port unconnected), it is recommended to fit a **120 Ω termination resistor** between pins 4 and 5 (A and B). For short buses (< 3 m total) it can be omitted.

### 4.4 Power Capacity

- Cat5e conductor cross-section: ~0.2 mm² (AWG 24)
- Safe current per pair: ~1.5 A
- Pins 1+2 (+24 V) = up to 3 A, pins 7+8 (auxiliary) = another 3 A
- Total: **up to 6 A at 24 V per cable** (144 W)
- 4× Nema 14 at 0.8 A = 3.2 A peak → one Cat5e cable is sufficient per box

> If using Nema 17 motors drawing >1 A, run a separate power cable or use two parallel patch cords.

---

## 5. Slave Firmware (MicroPython)

### 5.1 Installing MicroPython on Pico 2

1. Download MicroPython for RP2350:
   ```
   https://micropython.org/download/RPI_PICO2/
   ```
   File: `RPI_PICO2-xxxxxxxx-vX.X.X.uf2`

2. Connect the Pico 2 while holding the **BOOTSEL** button — it will appear as a USB drive.

3. Copy the `.uf2` file to the drive:
   ```bash
   cp RPI_PICO2-*.uf2 /media/$USER/RPI-RP2/
   ```
   The Pico will reboot automatically.

4. Verify the connection:
   ```bash
   ls /dev/ttyACM*
   ```

### 5.2 Uploading Slave Firmware

Use `mpremote` (recommended) or Thonny IDE.

**Install mpremote:**
```bash
pip install mpremote
```

**Upload files:**
```bash
cd /path/to/osamu/slave

mpremote connect /dev/ttyACM0

# Upload all files recursively
mpremote fs cp -r . :

# Or file by file:
mpremote fs mkdir :bus
mpremote fs mkdir :motor
mpremote fs mkdir :peripheral

mpremote fs cp config.py :config.py
mpremote fs cp main.py :main.py
mpremote fs cp state_machine.py :state_machine.py

mpremote fs cp bus/__init__.py :bus/__init__.py
mpremote fs cp bus/protocol.py :bus/protocol.py
mpremote fs cp bus/rs485.py :bus/rs485.py

mpremote fs cp motor/__init__.py :motor/__init__.py
mpremote fs cp motor/tmc2209.py :motor/tmc2209.py
mpremote fs cp motor/stepper.py :motor/stepper.py
mpremote fs cp motor/slot.py :motor/slot.py

mpremote fs cp peripheral/__init__.py :peripheral/__init__.py
mpremote fs cp peripheral/sensor.py :peripheral/sensor.py
mpremote fs cp peripheral/led.py :peripheral/led.py
```

**Reboot:**
```bash
mpremote reset
```

### 5.3 Sanity Check

```bash
mpremote connect /dev/ttyACM0 repl
```

In the REPL:
```python
from machine import Pin, UART
from bus.protocol import build_frame, CMD_PING, crc16_modbus

# Verify CRC
print(hex(crc16_modbus(b'123456789')))  # Expected: 0x4b37

# Check sensor (GP18)
sensor = Pin(18, Pin.IN, Pin.PULL_UP)
print(f"Sensor 0: {'triggered' if sensor.value() == 0 else 'open'}")

# Test LED
from peripheral.led import LEDStrip
leds = LEDStrip()
leds.set_slot(0, 1, 0, 255, 0)  # Slot 0 = green
leds.update()
```

### 5.4 Updating Firmware via Panel-Mount USB

After the box is assembled, the slave can be updated through the front-panel USB-C connector:
```bash
mpremote connect /dev/ttyACM0 fs cp config.py :config.py
mpremote reset
```

> No disassembly required.

---

## 6. Master Firmware (Klipper MCU)

### 6.1 Building Klipper Firmware for the Master

**Step 1: Clone Klipper (if not already cloned):**
```bash
cd ~
git clone https://github.com/Klipper3d/klipper.git
cd klipper
```

**Step 2: Copy the pico_mmu module:**
```bash
cp -r /path/to/osamu/master/pico_mmu/ ~/klipper/src/pico_mmu/
```

**Step 3: Add the module to the Klipper build system:**

Edit `~/klipper/src/Makefile` (or add a `Kconfig` entry):

```makefile
# In src/Makefile, add to the src-y section:
src-$(CONFIG_MACH_RP2040) += pico_mmu/protocol.c pico_mmu/rs485.c pico_mmu/command_bridge.c
```

> **Note:** The platform-abstraction functions in `rs485.c` currently use stubs. For production use, replace them with actual Klipper API calls (`gpio_out_setup`, `gpio_out_write`, Klipper UART functions). This is integration work for the final build stage.

**Step 4: Configure with make menuconfig:**
```bash
cd ~/klipper
make menuconfig
```

Select:
```
Micro-controller Architecture: Raspberry Pi RP2040/RP2350
Processor model: RP2350
Bootloader offset: No bootloader
Communication interface: USB
```

**Step 5: Build:**
```bash
make clean
make
```

Output: `~/klipper/out/klipper.uf2`

**Step 6: Flash the Master Pico 2:**
```bash
# Connect Pico 2 (Master) while holding BOOTSEL
cp ~/klipper/out/klipper.uf2 /media/$USER/RPI-RP2/
```

### 6.2 Verifying the Master MCU in Klipper

After flashing, the Master should appear as a serial device:
```bash
ls /dev/serial/by-id/usb-Klipper_rp2350_*
```

Typical path: `/dev/serial/by-id/usb-Klipper_rp2350_XXXXXXXXXXXXXXXX-if00`

---

## 7. Installing the Klipper Extras Module

### 7.1 Copy the extras file

```bash
cp /path/to/osamu/extras/pico_mmu.py ~/klipper/klippy/extras/pico_mmu.py
```

### 7.2 Restart Klipper

```bash
sudo systemctl restart klipper
```

### 7.3 Verify the module loaded

Check Klipper logs (`/tmp/klippy.log` or the Mainsail/Fluidd console):
```
Loaded MCU 'pico_mmu' ...
```

---

## 8. printer.cfg Configuration

Add to `printer.cfg`:

```ini
# ============================================================
# OSAMU (Pico-MMU) Configuration
# ============================================================

[mcu pico_mmu]
serial: /dev/serial/by-id/usb-Klipper_rp2350_XXXXXXXXXXXXXXXX-if00
# Replace XXXXX with the actual ID (from ls /dev/serial/by-id/)

[pico_mmu]
serial: pico_mmu
rs485_baud: 115200
tool_count: 8
polling_interval: 50

# Final filament sensor on Master
[filament_switch_sensor mmu_sensor]
switch_pin: pico_mmu:gpio3
pause_on_runout: False
# Runout is handled by the extras module, not the standard mechanism

# --- Slave Boxes ---

[pico_mmu_slave box_0]
# unique_id is determined on first MMU_HOME (see Klipper logs)
unique_id: 0000000000000000
slots: 0,1,2,3

[pico_mmu_slave box_1]
unique_id: 0000000000000000
slots: 4,5,6,7

# --- Fluidd / Mainsail buttons (see § Fluidd integration below) ---
[include osamu_macros.cfg]
```

> **Note:** the `[include]` expects `osamu_macros.cfg` to live next to your
> `printer.cfg`. Either copy or symlink it in:
> ```bash
> ln -s /path/to/osamu/klipper_extras/osamu_macros.cfg ~/printer_data/config/
> ```

### Fluidd / Mainsail integration

Once the include above is in place and Klipper has restarted, the macros panel
shows clickable buttons:

| Button | Action |
|---|---|
| `MMU_T0` … `MMU_T7` | One-click tool change |
| `MMU_BTN_HOME` | Initialize and enumerate slaves |
| `MMU_BTN_UNLOAD` | Retract the currently loaded filament |
| `MMU_BTN_STATUS` | Print MMU + per-slot state to the console |
| `MMU_BTN_DASH` | Formatted live state echo (uses `printer.pico_mmu`) |

`MMU_T0`..`MMU_T7` match the default `tool_count: 8`. Trim the include file or
extend it if your setup differs.

#### Live status object

The extras module exposes a status payload at `printer.pico_mmu`. Query it
from the Klipper host:

```bash
curl 'http://<klipper-host>:7125/printer/objects/query?pico_mmu' | jq
```

Sample response:

```json
{
  "result": {
    "status": {
      "pico_mmu": {
        "state": "idle",
        "current_tool": -1,
        "target_tool": -1,
        "tool_count": 8,
        "tools": [
          { "tool": 0, "slave_addr": 1, "online": true,
            "state": "LOADED", "color": "FF0000",
            "material": "PLA", "group": 0 }
        ],
        "boxes": [
          { "addr": 1, "online": true, "uid": "0102030405060708",
            "fw_version": "0.1", "slots": [0, 1, 2, 3] }
        ]
      }
    }
  }
}
```

Fluidd and Mainsail subscribe to printer-object updates over their WebSocket,
so changes to `printer.pico_mmu` propagate automatically — useful for custom
dashboard panels, runout indicators, or third-party tooling.

#### Verification

1. `FIRMWARE_RESTART` after editing `printer.cfg`.
2. Open Fluidd → **Macros**: confirm that `MMU_T0`..`MMU_T7` and the four
   `MMU_BTN_*` buttons are listed.
3. Click **MMU_BTN_HOME** and watch the console for `MMU: Home complete`.
4. Click **MMU_BTN_DASH** to see the live state echo.

---

## 9. First Boot and Verification

### 9.1 Power-On Sequence

1. **Apply 24 V power** to the bus (via RJ45 or a dedicated connector)
2. **Connect Master USB** to the Klipper host
3. **Start Klipper** — wait for "Ready"
4. **Verify Master MCU communication:**
   ```gcode
   FIRMWARE_RESTART
   ```
   Logs should show: `MCU 'pico_mmu' configured`

### 9.2 Enumeration (discover slaves)

```gcode
MMU_HOME
```

**Expected console output:**
```
MMU: Starting enumeration...
MMU: Slave 0123456789abcdef assigned addr 1
MMU: Home complete. 1 slaves online.
```

> Copy the `unique_id` from the log into the `[pico_mmu_slave]` section of `printer.cfg`.

### 9.3 Status Check

```gcode
MMU_STATUS
```

**Expected output:**
```
MMU State: idle, Current tool: T-1
  Box 0 [ONLINE] addr=1:
    T0: LOADED
    T1: EMPTY
    T2: LOADED
    T3: EMPTY
```

### 9.4 Test Load

```gcode
# Make sure filament is loaded into slot 0
MMU_LOAD TOOL=0
```

Expected behaviour:
1. Slot 0 LED → blue (pulsing)
2. Slot 0 motor runs → filament moves toward Master
3. Master microswitch triggers → motor switches to Assist mode
4. LED → solid cyan

### 9.5 Test Unload

```gcode
MMU_UNLOAD
```

### 9.6 Full Tool Change

```gcode
T0
# Wait for load...
T1
# T0 retracted, T1 loaded
```

---

## 10. Troubleshooting

### Slave does not respond to DISCOVER

| Check | How |
|---|---|
| 24 V on bus | Multimeter on RJ45 pins 1–2 |
| 5 V on Pico (DC-DC working) | Is the Pico LED on? Multimeter on VSYS |
| RS485 A/B correctly wired | A → pin 4, B → pin 5, not swapped |
| DE/RE on GP2 | Oscilloscope: GP2 should briefly go HIGH during a response |
| MicroPython is flashed | Connect USB, check REPL is accessible |
| main.py starts | In REPL: `import main` — no errors |

### TMC2209 does not initialise

| Check | How |
|---|---|
| VMOT 24 V present | Multimeter on VMOT–GND of TMC module |
| VIO 3.3 V present | From Pico 3V3 pin |
| MS1/MS2 correct | Addresses 0–3 must be unique |
| PDN/UART wired through 1 kΩ | Direct connection without resistor will not work |
| Capacitors installed | 100 µF + 100 nF per TMC |

**UART test in REPL:**
```python
from motor.tmc2209 import TMC2209Bank
bank = TMC2209Bank()
drv = bank.drivers[0]
print(drv._read_reg(0x01))  # GSTAT register — should return a number, not None
```

### Motor does not turn

| Check | How |
|---|---|
| EN pin LOW (active) | `Pin(14).value()` → should be 0 |
| STEP pulses present | Oscilloscope on GP6 |
| Coils connected | 4 motor wires: A1, A2, B1, B2 |
| Current sufficient | Increase `DEFAULT_RUN_CURRENT_MA` |

**Quick motor test:**
```python
from motor.stepper import Stepper
s = Stepper(0)
s.enable()
s.start(200, forward=True)  # 200 Hz = slow rotation
# Motor should turn
s.stop()
s.disable()
```

### Sensor never triggers

```python
from machine import Pin
p = Pin(18, Pin.IN, Pin.PULL_UP)
# Press the microswitch by hand
print(p.value())  # 0 = pressed, 1 = released
```

If always 0 — check whether the wire is shorted to GND.
