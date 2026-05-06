# OSAMU — Open Source Automatic Material Unit

OSAMU is a distributed multi-material filament feeding system for Klipper-based 3D printers. Rather than a single monolithic unit, it uses a **Master–Slave RS485 network** of autonomous 4-slot feeder boxes placed on an external filament storage rack, coordinated by a master node mounted on the printer's back panel.

---

## Architecture

```
 Klipper Host (RPi/SBC)
       │ USB-C
 ┌─────▼──────┐
 │   Master   │  RP2350 Pico 2 — back panel of printer
 │  (C/Klipper│  filament sensor (GPIO3)
 │   bridge)  │
 └─────┬──────┘
       │ RS485 (RJ45 daisy-chain, Cat5e)
       │
 ┌─────▼──────┐     ┌────────────┐     ┌────────────┐
 │  Slave #1  │────►│  Slave #2  │────►│  Slave #N  │
 │ 4 slots    │     │ 4 slots    │     │ 4 slots    │
 └────────────┘     └────────────┘     └────────────┘
```

| Layer | Hardware | Software | Location |
|---|---|---|---|
| **Host** | Raspberry Pi / SBC | Klipper extras (`pico_mmu.py`) | Running Klipper |
| **Master MCU** | RP2350 Pico 2 | C (Klipper MCU protocol) | Printer back panel |
| **Slave Boxes** | RP2350 Pico 2 | MicroPython | External filament rack |

### Slave Box Hardware (per 4-slot module)

- 1× Raspberry Pi Pico 2 (RP2350)
- 4× TMC2209 StepStick stepper drivers (UART-addressable)
- 4× Nema 14 (or 17) stepper motors
- 1× MAX485 RS485 transceiver
- 4× microswitch filament sensors
- 4× SK6812/WS2812B addressable LEDs (1 per slot)
- 1× DC-DC 24 V → 5 V converter (3 A+)
- 2× RJ45 jacks (daisy-chain IN/OUT)

### RJ45 Daisy-Chain Pinout (T568B)

| Pins | Colour | Purpose |
|---|---|---|
| 1, 2 | Orange | +24 V (motor power) |
| 3, 6 | Green | GND |
| 4, 5 | Blue | RS485 differential (A+, B−) |
| 7, 8 | Brown | Redundant +24 V / GND |

A single RJ45 cable carries ~6 A @ 24 V (≈144 W), enough for 4× Nema 14 motors.

---

## Communication Protocol

**RS485, 115200 baud, 8N1, half-duplex**

### Frame Format

```
[0xAA] [ADDR] [CMD] [SEQ] [LEN] [DATA (0–64 B)] [CRC16-LE]
```

- `ADDR` — `0x00` broadcast, `0x01–0xFE` slave, `0xFF` unassigned
- `CMD` — bit 7 = `0` request / `1` response
- `CRC` — CRC-16/MODBUS (little-endian)

### Command Set

| Cmd | Name | Purpose |
|---|---|---|
| `0x01` | `DISCOVER` | Broadcast — find unassigned slaves |
| `0x02` | `ASSIGN_ADDR` | Assign address by unique ID (saved to flash) |
| `0x03` | `PING` | Presence check, returns firmware version |
| `0x04` | `GET_CONFIG` | Read UID, address, firmware version, slot count |
| `0x05` | `GET_STATUS` | Poll all slot states and error flags |
| `0x06` | `SET_FILAMENT` | Set per-slot filament color and material |
| `0x07` | `SET_CURRENT` | Tune TMC run/hold current |
| `0x08` | `HOME_SLOT` | Feed until filament sensor triggers |
| `0x09` | `FEED` | Push filament toward extruder |
| `0x0A` | `SET_ASSIST` | Enable low-current friction-compensation mode |
| `0x0B` | `RETRACT` | Pull filament back |
| `0x0C` | `STOP` | Stop a single slot motor |
| `0x0D` | `STOP_ALL` | Emergency stop all motors |

### Slot States

```
EMPTY ──► LOADED ──► FEEDING ──► (sensor triggers) ──► ...
                     RETRACTING ──► (sensor clears) ──► EMPTY
                     ASSIST ──────────────────────────► (printing)
                     ERROR ───────────────────────────► (jam / timeout)
```

---

## Slave Firmware (MicroPython, RP2350)

Runs on both cores:

| Core | Responsibilities |
|---|---|
| **Core 0** | `uasyncio` event loop: RS485 RX, slot state machines, sensor debounce, watchdog |
| **Core 1** | `_thread`: TMC2209 UART init, PIO step-generator setup, LED timing |

**Key features:**
- **PIO stepping** — 4 independent PIO state machines generate step pulses in hardware (1 Hz – 50 kHz, zero Python jitter).
- **StallGuard jam detection** — TMC2209 back-EMF sensing stops motors on filament jam and sets the slot to `ERROR`.
- **Watchdog** — if the master is silent for >5 s, all motors stop/disable and all LEDs blink red.
- **LED status indicators** per slot: Off (empty), solid green (loaded), breathing blue (feeding/retracting), solid cyan (assist), blinking red (error).

```
slave/
├── main.py              # Entry point, hardware init
├── state_machine.py     # SlaveController — dispatches commands, watchdog
├── bus/
│   ├── protocol.py      # Frame codec, CRC-16/MODBUS
│   └── rs485.py         # Half-duplex UART (TX DE pin control)
├── motor/
│   ├── slot.py          # Per-slot state machine + timeout logic
│   ├── stepper.py       # PIO-based step generator
│   └── tmc2209.py       # TMC2209 UART driver (CRC-8, StallGuard)
└── peripheral/
    ├── sensor.py         # Filament microswitches, 20 ms debounce
    └── led.py            # SK6812 PIO driver, per-slot animations
```

---

## Master Firmware (C, Klipper MCU)

Bridges the Klipper host to the RS485 slave network via USB-C:

```
master/pico_mmu/
├── protocol.c/h         # Frame codec (mirrors Python implementation)
├── rs485.c              # UART + DE pin driver, RX ring buffer
└── command_bridge.c     # Klipper command handlers:
                         #   config_pmu_rs485 / pmu_rs485_send / pmu_rs485_query
```

---

## Klipper Extras (`extras/pico_mmu.py`)

Runs on the Klipper host. Provides G-code commands and orchestrates the full tool-change flow.

### G-code Commands

| Command | Description |
|---|---|
| `MMU_HOME` | Enumerate and initialise all slaves |
| `MMU_STATUS` | Print current state and slot configuration |
| `MMU_CHANGE_TOOL TOOL=N` | Full unload → load tool change |
| `MMU_LOAD TOOL=N` | Load a tool without unloading the current one |
| `MMU_UNLOAD` | Retract the current tool |
| `MMU_SELECT TOOL=N` | Select a tool for manual placement |

### Tool-Change Flow (`T0 → T1` example)

1. **Unload T0** — extruder tip-forms; slave retracts at 1000 Hz until sensor clears (30 s timeout).
2. **Load T1** — slave feeds at 800 Hz until master's filament sensor triggers (60 s timeout).
3. **Assist mode** — slave switches to 150 mA; extruder pulls filament into nozzle at 300 mm/s. Printing resumes.

### Web Panel (Fluidd/Mainsail)

A standalone visual panel provides Bambu Lab AMS-style slot management.

**Installation:**

1. **Symlink the Moonraker component:**
   ```bash
   ln -s /path/to/osamu/klipper_extras/mmu_panel.py ~/moonraker/moonraker/components/
   ```

2. **Add to `moonraker.conf`:**
   ```ini
   [mmu_panel]
   path: /path/to/osamu/klipper_extras/web_panel
   ```

3. **Restart Moonraker:**
   ```bash
   sudo systemctl restart moonraker
   ```

4. **Access the panel:**
   ```
   http://<printer-ip>/server/mmu/
   ```

**Fluidd Integration (as camera iframe):**

Option A — via UI:
1. Open Fluidd → Settings (⚙️) → Cameras
2. Click **Add Camera**
3. Configure:
   - Name: `MMU`
   - URL: `/server/mmu/`
   - Camera Service: `iframe`
4. The panel will appear in the camera section of the dashboard

Option B — via `.fluidd.json` (in `~/printer_data/config/`):
```json
{
  "cameras": [
    {
      "name": "MMU",
      "url": "/server/mmu/",
      "service": "iframe"
    }
  ]
}
```
Then restart Fluidd or reload the page.

**Features:**
- Real-time slot status via WebSocket
- Click slot to change tool
- Edit filament color and material per slot
- Home/Unload buttons
- Infinite spool group indicators

### Automatic Enumeration

Slaves ship with address `0xFF` (unassigned). Each RP2350 has a unique 8-byte ROM ID. On `MMU_HOME`:
1. Master broadcasts `DISCOVER`; unassigned slaves respond after a random 0–63 ms backoff.
2. Master matches the ROM ID from `printer.cfg` and sends `ASSIGN_ADDR`.
3. Address is saved to the slave's flash — no manual address jumpers needed.

---

## Documentation

| Document | Description |
|---|---|
| [docs/hardware-spec.md](docs/hardware-spec.md) | System architecture, hardware components, wiring, and mechanical design |
| [docs/firmware-spec.md](docs/firmware-spec.md) | RS485 protocol reference, slave/master firmware architecture, Klipper extras, sequence diagrams |
| [docs/build-guide.md](docs/build-guide.md) | Step-by-step assembly, wiring diagrams, firmware flashing, Klipper configuration, and troubleshooting |

---

## Repository Layout

```
docs/
  hardware-spec.md  # Hardware architecture and wiring
  firmware-spec.md  # Protocol and firmware specification
  build-guide.md    # Step-by-step build and setup guide
klipper_extras/  # Klipper extras module (pico_mmu.py)
  osamu_macros.cfg  # Macro buttons for Fluidd/Mainsail
  web_panel/       # Visual MMU panel (HTML/JS/CSS)
master/          # Master MCU firmware (C)
  pico_mmu/
slave/           # Slave box firmware (MicroPython)
tests/           # Protocol unit tests
```

---

## License

See [LICENSE](LICENSE).
