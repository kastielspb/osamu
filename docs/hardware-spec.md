# Hardware Specification: OSAMU Distributed Filament Feeding System

## 1. System Architecture

The system follows a decentralised Master–Slave network topology. Instead of a single complex mechanical device, it uses autonomous modules (boxes) connected by a shared data and power bus.

| Component | Role | Location |
|---|---|---|
| Orchestrator (Master) | Coordinates slaves, interfaces with Klipper, monitors the final filament sensor | Printer back panel (replaces stock filament sensor) |
| Satellite (Slave Box) | Local control of 4 filament slots (motors, sensors, LEDs) | External filament storage rack |
| Cascading splitter | Mechanical 4-to-1 PTFE path merging | Level 1: inside each slave box; Level 2: inside the Master |
| Bus (Backbone) | 24 V power and RS485 data transport | Standard RJ45 patch cables (daisy-chained) |

---

## 2. Hardware

### Master Node (Orchestrator)

- **Microcontroller:** Raspberry Pi Pico 2 (RP2350)
- **Interfaces:**
  - USB-C input (to Klipper host)
  - RJ45 output (start of the RS485 bus to slaves)
  - 24 V power input (from printer PSU)
- **Sensor:** Microswitch on the output of the final splitter
- **Splitter:** Printed 4-to-1 block (Nylon/SLA) with PC4-M6 fittings on inputs and output

### Satellite Node (Slave Box — 4-slot module)

- **Microcontroller:** Raspberry Pi Pico 2 (RP2350)  
  Panel-mount USB-C extension for in-place firmware updates without disassembly
- **Motor drivers:** 4× TMC2209 (UART-addressable for current tuning and StallGuard)
- **Motors:** 4× Nema 14 (or Nema 17) stepper motors
- **Communication:** RS485 transceiver (MAX485 / SP485)
- **Power:** DC-DC step-down converter (24 V → 5 V) to power logic from the power bus
- **Peripherals:**
  - 4× microswitch filament sensors (slot inputs)
  - 16× SK6812 / WS2812B addressable LEDs (slot status indication)
  - 2× RJ45 jacks (IN and OUT) for daisy-chain passthrough

### Interconnect Bus Pinout (RJ45 / T568B)

| Pair | Pins | Purpose | Description |
|---|---|---|---|
| Orange | 1, 2 | +24 V | Main motor power |
| Green | 3, 6 | GND | Common ground (power + signal) |
| Blue | 4, 5 | RS485 | Differential data pair (A and B) |
| Brown | 7, 8 | GND / +24 V | Auxiliary conductors for voltage droop compensation |

---

## 3. Software Overview

- **Slave language:** MicroPython
- **Async model:** `uasyncio` for non-blocking network, motor, and sensor handling
- **Step generation:** RP2350 PIO (Programmable I/O) co-processors for hardware step-pulse generation, offloading the Python interpreter entirely
- **Communication protocol:** Half-duplex RS485 (Master Polling), 115200 baud
- **Frame structure:**

  ```
  [START_BYTE] [DEVICE_ID] [CMD] [LENGTH] [DATA...] [CRC16]
  ```

- **Slave multi-core split:**
  - **Core 0:** Handles RS485 bus and high-level slot state machine logic
  - **Core 1 / PIO:** Manages TMC2209 UART drivers and WS2812B LED animation timing

---

## 4. Mechanics and Synchronisation Strategy

### Filament Path Geometry

- **Channel angle:** Merging angles inside all splitters must not exceed 15–20° to prevent jamming
- **Fittings:** All PTFE tubes (spool → box, box → Master) are secured exclusively with pneumatic fittings (PC4-M6) to eliminate play during retracts

### Print-time Synchronisation (Friction Compensation / Assist Mode)

Hard step-for-step synchronisation between the slave motor and the printer extruder is **not used**, to avoid cumulative error and filament deformation.

- **Load / unload phase:** The slave motor runs at full operating current (e.g. 0.8 A), actively pushing or pulling the filament until the sensor at the Master splitter triggers.
- **Print phase (Assist Mode):** On command from the Master, the slave reduces TMC2209 current (e.g. to 0.1–0.2 A). The motor continuously applies a light push in the feed direction, compensating PTFE tube friction without independently extruding. The printer extruder provides all the actual pulling force.
- **Mechanical buffer (optional):** A spring-loaded tension-compensation lever can be integrated into the slave box design. Its actuation locally adjusts the feed motor speed or current via the slave board.
