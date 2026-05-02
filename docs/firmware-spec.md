# Firmware and Protocol Specification

## Architecture

Three-tier system:

| Layer | Component | Language | Location |
|---|---|---|---|
| 1 | **Klipper extras module** | Python | Host (Raspberry Pi / SBC) |
| 2 | **Master MCU firmware** | C (Klipper MCU) | RP2350 on the printer back panel |
| 3 | **Slave firmware** | MicroPython | RP2350 in each slave box |

Interaction: Klipper extras ↔ Master MCU (USB CDC, Klipper protocol) ↔ Slave×N (RS485, binary protocol)

---

## Sequence Diagrams

### Scenario 1: System Initialisation (MMU_HOME)

```mermaid
sequenceDiagram
    participant K as Klipper Host
    participant E as Extras (pico_mmu.py)
    participant M as Master MCU
    participant S1 as Slave Box 0
    participant S2 as Slave Box 1

    K->>E: MMU_HOME (G-code)
    Note over E: Start auto-enumeration

    E->>M: config_rs485(uart=1, baud=115200)
    M-->>E: OK

    loop Discovery cycle (until 3 empty)
        E->>M: rs485_send(addr=0xFF, DISCOVER)
        M->>S1: [0xAA][0xFF][0x02][seq][0x00][CRC]
        M->>S2: [0xAA][0xFF][0x02][seq][0x00][CRC]
        Note over S1: random backoff 12 ms
        Note over S2: random backoff 38 ms
        S1-->>M: [0xAA][0xFF][0x82][seq][0x08][unique_id_1][CRC]
        M-->>E: rs485_rx(addr=0xFF, DISCOVER, unique_id_1)
        S2-->>M: [0xAA][0xFF][0x82][seq][0x08][unique_id_2][CRC]
        M-->>E: rs485_rx(addr=0xFF, DISCOVER, unique_id_2)
    end

    Note over E: Match unique_id with printer.cfg

    E->>M: rs485_send(addr=0xFF, ASSIGN_ADDR, uid_1 + 0x01)
    M->>S1: ASSIGN_ADDR(unique_id_1, addr=1)
    S1-->>M: [status: OK]
    Note over S1: Save addr=1 to flash
    M-->>E: rs485_rx(OK)

    E->>M: rs485_send(addr=0xFF, ASSIGN_ADDR, uid_2 + 0x02)
    M->>S2: ASSIGN_ADDR(unique_id_2, addr=2)
    S2-->>M: [status: OK]
    Note over S2: Save addr=2 to flash
    M-->>E: rs485_rx(OK)

    E->>M: rs485_query(addr=1, GET_STATUS)
    M->>S1: GET_STATUS
    S1-->>M: [LOADED, EMPTY, LOADED, EMPTY][sensors][errors]
    M-->>E: slot_states

    E->>M: rs485_query(addr=2, GET_STATUS)
    M->>S2: GET_STATUS
    S2-->>M: [LOADED, LOADED, EMPTY, EMPTY][sensors][errors]
    M-->>E: slot_states

    E-->>K: "MMU: Home complete. 2 slaves online."
```

### Scenario 2: Tool Change (T0 → T1)

```mermaid
sequenceDiagram
    participant Slicer as G-code (slicer)
    participant K as Klipper Host
    participant E as Extras (pico_mmu.py)
    participant M as Master MCU
    participant S as Slave Box 0
    participant Sensor as Master Sensor

    Slicer->>K: T1
    K->>E: MMU_CHANGE_TOOL TOOL=1
    Note over E: state = UNLOADING

    rect rgb(255, 240, 240)
        Note over E,S: Phase 1: Unload current tool (T0, slot 0)
        E->>K: G1 E-5 F3600 (quick retract)
        E->>K: G1 E2 F1800 (pause)
        E->>K: G1 E-15 F3000 (long retract — tip forming)
        Note over E: state = TIP_FORMING

        E->>M: rs485_send(addr=1, RETRACT, slot=0, speed=1000)
        M->>S: RETRACT(slot=0, speed=1000 Hz)
        S-->>M: [status: OK]
        Note over S: Motor 0: reverse at 1000 Hz, current 0.8 A
        Note over S: LED slot 0: blue pulsing
        M-->>E: OK

        loop Poll until EMPTY (timeout 30 s)
            E->>M: rs485_query(addr=1, GET_STATUS)
            M->>S: GET_STATUS
            S-->>M: [RETRACTING, LOADED, -, -]
            M-->>E: slot_states
            Note over E: slot 0 still RETRACTING...
        end

        Note over S: Sensor slot 0 open → state = EMPTY
        Note over S: Motor 0: stop. LED: off
        E->>M: rs485_query(addr=1, GET_STATUS)
        M->>S: GET_STATUS
        S-->>M: [EMPTY, LOADED, -, -]
        M-->>E: slot 0 = EMPTY ✓
    end

    rect rgb(240, 255, 240)
        Note over E,Sensor: Phase 2: Load new tool (T1, slot 1)
        Note over E: state = LOADING

        E->>M: rs485_send(addr=1, FEED, slot=1, speed=800)
        M->>S: FEED(slot=1, speed=800 Hz)
        S-->>M: [status: OK]
        Note over S: Motor 1: forward at 800 Hz, current 0.8 A
        Note over S: LED slot 1: blue pulsing
        M-->>E: OK

        loop Wait for Master Sensor (timeout 60 s)
            Note over S: Filament travelling through tube...
            Sensor-->>M: GPIO3 = LOW (filament detected!)
            M-->>K: endstop triggered
            K-->>E: filament_present = True
        end

        Note over E: Master sensor triggered!
    end

    rect rgb(240, 240, 255)
        Note over E,S: Phase 3: Switch to Assist Mode
        E->>M: rs485_send(addr=1, SET_ASSIST, slot=1, current=150mA)
        M->>S: SET_ASSIST(slot=1, 150 mA)
        Note over S: TMC2209 slot 1: current reduced to 150 mA
        Note over S: Motor 1: light push (friction compensation)
        Note over S: LED slot 1: solid cyan
        S-->>M: [status: OK]
        M-->>E: OK
    end

    E->>K: G92 E0 #59; G1 E30 F300 (load into hotend)
    Note over E: state = PRINTING
    E-->>K: "Tool change T0→T1 complete"
    K->>Slicer: Resume print
```

### Scenario 3: Normal Polling (during print)

```mermaid
sequenceDiagram
    participant E as Extras
    participant M as Master MCU
    participant S1 as Slave 1 (active)
    participant S2 as Slave 2 (idle)

    loop Every 50 ms (~20 Hz)
        E->>M: rs485_query(addr=1, GET_STATUS)
        M->>S1: GET_STATUS
        S1-->>M: [EMPTY, ASSIST, LOADED, EMPTY][0x02][0x00]
        M-->>E: slot_states OK
        Note over E: Slot 1 in ASSIST — nominal

        E->>M: rs485_query(addr=2, GET_STATUS)
        M->>S2: GET_STATUS
        S2-->>M: [LOADED, LOADED, EMPTY, EMPTY][0x03][0x00]
        M-->>E: slot_states OK
    end

    Note over S1: Watchdog reset (last_poll = now)
    Note over S2: Watchdog reset (last_poll = now)
```

### Scenario 4: Jam Detection

```mermaid
sequenceDiagram
    participant E as Extras
    participant M as Master MCU
    participant S as Slave Box 0
    participant TMC as TMC2209 (slot 1)
    participant K as Klipper Host

    Note over S: Slot 1 in FEEDING state
    Note over S: StallGuard polled every 200 ms

    S->>TMC: read SG_RESULT
    TMC-->>S: SG_RESULT = 8 (threshold = 20)
    Note over S: SG_RESULT < THRESHOLD → JAM!
    Note over S: Motor 1: STOP
    Note over S: Slot 1: state = ERROR(JAM)
    Note over S: LED slot 1: red blinking

    E->>M: rs485_query(addr=1, GET_STATUS)
    M->>S: GET_STATUS
    S-->>M: [EMPTY, ERROR, LOADED, EMPTY][0x04][0x03]
    M-->>E: slot 1 = ERROR, error_code = JAM

    Note over E: JAM error detected!
    E->>M: rs485_send(addr=1, STOP_ALL)
    M->>S: STOP_ALL
    S-->>M: OK
    Note over S: All motors stopped

    E->>K: PAUSE (pause print)
    E->>K: RESPOND MSG="MMU ERROR: Jam detected on T1 (slave 1, slot 1)"
    Note over K: Printer paused, awaiting user intervention

    Note over K: User clears jam, presses Resume
    K->>E: MMU_HOME (re-initialise)
    E->>M: rs485_query(addr=1, GET_STATUS)
    S-->>M: [EMPTY, LOADED, LOADED, EMPTY]
    Note over E: Slot 1 LOADED again — ready to continue
```

### Scenario 5: Filament Runout

```mermaid
sequenceDiagram
    participant E as Extras
    participant M as Master MCU
    participant S as Slave Box 0
    participant K as Klipper Host

    Note over S: Slot 1 in ASSIST state (printing T1)
    Note over S: Spool runs out

    Note over S: Sensor slot 1 opens
    Note over S: Slot 1: ASSIST → EMPTY (sensor cleared)
    Note over S: Motor 1: stop (nothing to feed)
    Note over S: LED slot 1: off

    E->>M: rs485_query(addr=1, GET_STATUS)
    M->>S: GET_STATUS
    S-->>M: [EMPTY, EMPTY, LOADED, EMPTY][0x04][0x00]
    M-->>E: slot 1 = EMPTY (was ASSIST!)

    Note over E: Runout detected! Slot 1 was active → spool empty

    alt Another slot available (T2 = LOADED)
        E->>K: RESPOND MSG="MMU: Runout T1, auto-switching to T2"
        E->>E: MMU_CHANGE_TOOL TOOL=2
        Note over E: Full tool-change cycle executed (see Scenario 2)
    else No slots available
        E->>K: PAUSE
        E->>K: RESPOND MSG="MMU: Runout T1, no filament available. Load filament and resume."
    end
```

### Scenario 6: Connection Loss (Watchdog)

```mermaid
sequenceDiagram
    participant E as Extras
    participant M as Master MCU
    participant S as Slave Box 0
    participant K as Klipper Host

    Note over M: USB disconnected / Klipper restart
    Note over S: Last GET_STATUS was 2 seconds ago...

    loop Watchdog check (every 500 ms)
        Note over S: elapsed = now - last_poll
        Note over S: 2.5 s... 3 s... 3.5 s... 4 s... 4.5 s...
    end

    Note over S: elapsed > 5000 ms → WATCHDOG TRIGGERED!
    Note over S: ⚠ All motors: STOP + DISABLE
    Note over S: ⚠ All slots → ERROR
    Note over S: ⚠ All LEDs → red blinking

    Note over M: ...connection restored

    E->>M: rs485_query(addr=1, GET_STATUS)
    M->>S: GET_STATUS
    S-->>M: [ERROR, ERROR, ERROR, ERROR][...][...]
    Note over S: Watchdog reset (connection restored)

    E->>K: RESPOND MSG="MMU: Slave 1 watchdog recovery"
    E->>M: rs485_send(addr=1, STOP_ALL)
    Note over E: MMU_HOME required to recover
```

### Scenario 7: Set Filament Color

```mermaid
sequenceDiagram
    participant U as User / Slicer
    participant K as Klipper Host
    participant E as Extras (pico_mmu.py)
    participant SV as save_variables
    participant M as Master MCU
    participant S as Slave Box 0

    U->>K: MMU_SET_FILAMENT_COLOR TOOL=1 COLOR=FF3300
    K->>E: cmd_MMU_SET_FILAMENT_COLOR(tool=1, color="FF3300")

    Note over E: Validate tool range and parse hex color
    Note over E: _tool_colors[1] = (255, 51, 0)

    E->>M: rs485_send(addr=1, SET_FILAMENT_COLOR, slot=1, r=255, g=51, b=0)
    M->>S: SET_FILAMENT_COLOR(slot=1, r=255, g=51, b=0)
    Note over S: _filament_colors[1] = (255, 51, 0)
    alt Slot 1 is LOADED
        Note over S: _update_slot_led(1) → LED solid (255, 51, 0) immediately
    else Slot 1 is EMPTY / FEEDING / other
        Note over S: Color stored #59; LED unchanged until slot returns to LOADED
    end
    S-->>M: [status: OK]
    M-->>E: OK

    E->>SV: save mmu_tool_colors[1] = [255, 51, 0]
    Note over SV: Written to variables.cfg (survives restart)

    E-->>K: "MMU: Tool T1 color set to #FF3300"
    K-->>U: respond_info

    Note over E,S: On next MMU_HOME — color is re-pushed after ASSIGN_ADDR
```

### Scenario 8: System Block Diagram

```mermaid
flowchart TB
    subgraph HOST["Host (Raspberry Pi)"]
        KL[Klipper klippy]
        EX[pico_mmu.py extras]
        MR[Moonraker / Mainsail]
    end

    subgraph MASTER["Master RP2350"]
        MCU[Klipper MCU firmware]
        RS[RS485 Bridge]
        FS[Filament Sensor<br/>GPIO3 → endstop]
    end

    subgraph BUS["RS485 Bus (RJ45 Daisy Chain)"]
        WIRE[24 V + GND + RS485 A/B]
    end

    subgraph SLAVE1["Slave Box 0"]
        SM1[State Machine<br/>uasyncio Core 0]
        PIO1[PIO Step Gen<br/>Core 1]
        TMC1[TMC2209 ×4<br/>UART addr 0-3]
        MOT1[Nema14 ×4]
        SEN1[Microswitches ×4]
        LED1[SK6812 ×16]
        SP1[4-to-1 Splitter]
    end

    subgraph SLAVE2["Slave Box 1"]
        SM2[State Machine]
        PIO2[PIO Step Gen]
        TMC2_[TMC2209 ×4]
        MOT2[Nema14 ×4]
        SEN2[Microswitches ×4]
        LED2[SK6812 ×16]
        SP2[4-to-1 Splitter]
    end

    MR <-->|HTTP/WebSocket| KL
    KL <-->|MCU protocol| EX
    EX <-->|USB CDC| MCU
    MCU --- RS
    MCU --- FS
    RS <-->|RS485| WIRE
    WIRE <-->|RS485| SM1
    WIRE <-->|RS485| SM2
    SM1 --> PIO1 --> TMC1 --> MOT1
    SM1 --> LED1
    SEN1 --> SM1
    MOT1 -->|filament| SP1

    SM2 --> PIO2 --> TMC2_ --> MOT2
    SM2 --> LED2
    SEN2 --> SM2
    MOT2 -->|filament| SP2

    SP1 -->|PTFE tube| FS
    SP2 -->|PTFE tube| FS
    FS -->|to extruder| EXT[Printer Extruder]
```

---

## 1. RS485 Protocol (Master ↔ Slave)

### 1.1 Physical / Link Layer

- Half-duplex RS485, **115200 baud, 8N1**
- Topology: **Master Polling** — slaves transmit only in response to a master request
- Response timeout: **10 ms**, retries: up to **3**, then mark slave as OFFLINE

### 1.2 Frame Format

```
┌──────────┬──────┬─────┬─────┬─────┬──────────────┬────────┐
│ PREAMBLE │ ADDR │ CMD │ SEQ │ LEN │     DATA     │ CRC16  │
│  1 byte  │  1   │  1  │  1  │  1  │  0–64 bytes  │ 2 bytes│
└──────────┴──────┴─────┴─────┴─────┴──────────────┴────────┘
```

| Field | Size | Description |
|---|---|---|
| PREAMBLE | 1 | `0xAA` — frame start marker |
| ADDR | 1 | `0x00` = broadcast, `0x01–0xFE` = slave address, `0xFF` = unassigned |
| CMD | 1 | Command code; **bit 7**: `0` = request (Master→Slave), `1` = response (Slave→Master) |
| SEQ | 1 | Sequence number; slave echoes the same SEQ in its response |
| LEN | 1 | Length of the DATA field (0–64) |
| DATA | 0–64 | Payload (command-specific) |
| CRC16 | 2 | CRC-16/MODBUS over all preceding bytes, little-endian |

### 1.3 Command Set

**Direction: Master → Slave (bit 7 = 0)**

| CMD | Name | Request DATA | Response DATA | Description |
|---|---|---|---|---|
| `0x01` | PING | — | `[fw_ver:2]` | Presence check, returns firmware version |
| `0x02` | DISCOVER | — | `[unique_id:8]` | Find unassigned slaves (addr=0xFF) |
| `0x03` | ASSIGN_ADDR | `[unique_id:8][new_addr:1]` | `[status:1]` | Assign address to slave |
| `0x04` | GET_STATUS | — | `[slot_states:4][sensors:1][errors:1]` | State of all 4 slots |
| `0x05` | FEED | `[slot:1][speed_hz:2]` | `[status:1]` | Start feeding from slot |
| `0x06` | RETRACT | `[slot:1][speed_hz:2]` | `[status:1]` | Retract filament in slot |
| `0x07` | SET_ASSIST | `[slot:1][current_ma:2]` | `[status:1]` | Enable Assist Mode |
| `0x08` | STOP | `[slot:1]` | `[status:1]` | Stop one slot motor |
| `0x09` | STOP_ALL | — | `[status:1]` | Emergency stop all motors |
| `0x0A` | SET_LED | `[mode:1][slot:1][r:1][g:1][b:1]` | `[status:1]` | Control WS2812B LEDs |
| `0x0B` | GET_CONFIG | — | `[config_blob:N]` | Read slave configuration |
| `0x0C` | SET_CURRENT | `[slot:1][run_ma:2][hold_ma:2]` | `[status:1]` | Configure TMC2209 current |
| `0x0D` | HOME_SLOT | `[slot:1]` | `[status:1]` | Feed until sensor triggers |
| `0x0E` | SET_FILAMENT_COLOR | `[slot:1][r:1][g:1][b:1]` | `[status:1]` | Set filament color for a slot; updates LED immediately if slot is LOADED |

**Status codes (`status` field in responses):**

| Code | Constant | Description |
|---|---|---|
| `0x00` | OK | Command accepted / completed |
| `0x01` | BUSY | Slot is busy with another operation |
| `0x02` | ERROR_SLOT_EMPTY | No filament in slot |
| `0x03` | ERROR_JAM | Jam detected |
| `0x04` | ERROR_TIMEOUT | Operation timed out |
| `0x05` | ERROR_INVALID_SLOT | Slot number out of range |
| `0xFF` | UNKNOWN_CMD | Unknown command |

**Slot states (`slot_states` field in GET_STATUS, 1 byte × 4 slots):**

| Code | Constant | Description |
|---|---|---|
| `0x00` | EMPTY | No filament (sensor not triggered) |
| `0x01` | LOADED | Filament present, motor idle |
| `0x02` | FEEDING | Active feed |
| `0x03` | RETRACTING | Retract in progress |
| `0x04` | ASSIST | Friction-compensation mode (low current) |
| `0x05` | ERROR | Error (jam / timeout) |

### 1.4 Auto-Enumeration Protocol

Executed on every system start (`MMU_HOME`):

```
1. Master: broadcast DISCOVER (addr=0xFF)
2. Unassigned slaves (addr=0xFF): respond after random backoff 0–50 ms,
   payload = unique_id (8 bytes, RP2350 ROM ID)
3. Master: records unique_id
   - Collision (CRC fail or two simultaneous responses) → retry DISCOVER for this cycle
4. Master: ASSIGN_ADDR(unique_id, new_addr) for each discovered slave
5. Slave: saves address to flash (rp2350 flash NVS), responds OK
6. Repeat steps 1–5 until 3 consecutive DISCOVER cycles return empty
```

### 1.5 Polling Cycle (normal operation)

```python
# pseudocode, ~20 Hz
while True:
    for addr in assigned_slaves:
        resp = send_recv(GET_STATUS, addr, timeout_ms=10)
        if resp is None:
            slaves[addr].offline_count += 1
            if slaves[addr].offline_count > 3:
                raise_alarm(f"Slave {addr} OFFLINE")
        else:
            slaves[addr].update(resp)
            slaves[addr].offline_count = 0
    await asyncio.sleep_ms(50)
```

---

## 2. Slave Firmware (MicroPython, RP2350)

### 2.1 File Structure

```
slave/
├── main.py                  # Entry point: starts Core 0 (asyncio) and Core 1 (_thread)
├── config.py                # Pins, default address, currents, timeouts
├── bus/
│   ├── protocol.py          # Frame encode/decode, CRC16
│   └── rs485.py             # UART + DE/RE pin management
├── motor/
│   ├── tmc2209.py           # TMC2209 UART driver: registers, current, StallGuard
│   ├── stepper.py           # PIO-based step generator (frequency, direction)
│   └── slot.py              # Per-slot state machine
├── peripheral/
│   ├── sensor.py            # Microswitches: debounce, IRQ
│   └── led.py               # WS2812B via PIO
└── state_machine.py         # Coordinator for 4-slot state machines
```

### 2.2 Core Assignment

| Core | Tasks |
|---|---|
| **Core 0** | `uasyncio` event loop: RS485 receive/parse, slot state machines, sensor IRQ, watchdog timer |
| **Core 1** | `_thread`: PIO step programs (4 SMs), TMC2209 UART configuration, WS2812B timing |

**RP2350 PIO resources:** 2 blocks × 4 SMs = 8 SMs total.  
Allocation: 4 SMs for step generation (one per slot), 1 SM for WS2812B, 3 SMs reserved.

### 2.3 Slot State Machine

```
         [sensor triggered]
EMPTY ──────────────────────→ LOADED
                                │
              [FEED cmd]        │  [RETRACT cmd]
FEEDING ←────────────────────── │ ────────────────────→ RETRACTING
   │       [master sensor OK]   │   [sensor cleared]          │
   └──────→ LOADED ←────────────┘                             ↓ EMPTY
   │                            │
   │ [timeout/jam]              │ [SET_ASSIST]
   ↓                            ↓
ERROR ←──────────────────── ASSIST ──[STOP]──→ LOADED
   │
   └──[STOP]──→ LOADED/EMPTY (depending on sensor)

STOP_ALL → stops motor; new state = LOADED or EMPTY based on sensor
```

### 2.4 Watchdog (safety)

If the Master has not polled the slave (`GET_STATUS` not received) for more than **5 seconds**, the slave automatically:
1. Stops all motors
2. Extinguishes active LED animations
3. Moves all slots to `ERROR`

Implemented as a `uasyncio.create_task` that checks `last_poll_time`.

### 2.5 StallGuard (jam detection)

TMC2209 is read via UART every 200 ms (while FEED or RETRACT is active):
- If `SG_RESULT < SG_THRESHOLD` → slot transitions to `ERROR(JAM)`
- `SG_THRESHOLD` is set in `config.py`, default `20`

---

## 3. Master MCU Firmware (C, Klipper MCU)

### 3.1 Overview

Patch to `klipper/src/` for RP2350. Built with the standard Klipper `make` + `menuconfig` (target: RP2350, USB CDC). Adds the `pico_mmu/` module.

### 3.2 File Structure (additions to klipper/src)

```
klipper/src/pico_mmu/
├── rs485.c              # UART init, TX/RX buffers, DE/RE management
├── rs485.h
├── protocol.c           # CRC-16/MODBUS, frame encode/decode
├── protocol.h
└── command_bridge.c     # Klipper MCU command registration
```

### 3.3 Klipper MCU Commands (registered in command_bridge.c)

| Command | Parameters | Description |
|---|---|---|
| `config_rs485` | `bus`, `tx_pin`, `rx_pin`, `de_pin`, `baud` | Initialise RS485 interface |
| `rs485_send` | `addr`, `cmd`, `seq`, `data` | Send frame to slave |
| `rs485_query` | `addr`, `cmd`, `seq`, `data`, `timeout` | Send and wait for response |

Slave responses are forwarded to the host via the standard Klipper response callback.

### 3.4 Master RP2350 Pin Assignment

| Pin | Purpose |
|---|---|
| USB-C | Klipper host (USB CDC) |
| UART1 TX / RX | RS485 transceiver (MAX485 / SP485) |
| GPIO (DE) | RS485 direction enable |
| GPIO (SENSOR) | Final splitter microswitch (endstop) |

The sensor is registered as a standard Klipper endstop — accessible via `[filament_switch_sensor]` in `printer.cfg`.

---

## 4. Klipper Extras Module (Python, host)

### 4.1 File

```
klipper/klippy/extras/pico_mmu.py
```

### 4.2 G-code Commands

| Command | Parameters | Description |
|---|---|---|
| `MMU_HOME` | — | Auto-enumeration, initialise all slaves |
| `MMU_STATUS` | — | Print state of all slots |
| `MMU_CHANGE_TOOL` | `TOOL=N` | Full filament change cycle |
| `MMU_LOAD` | `TOOL=N` | Load without changing (if slot already selected) |
| `MMU_UNLOAD` | — | Retract current filament |
| `MMU_SELECT` | `TOOL=N` | Select slot without loading |
| `MMU_SET_FILAMENT_COLOR` | `TOOL=N COLOR=RRGGBB` | Set filament color for a tool slot; pushes `SET_FILAMENT_COLOR` to the slave and persists via `[save_variables]`; colors are re-pushed to slaves on every `MMU_HOME` |

`T0`, `T1`, … `TN` — standard Klipper tool-change commands, forwarded to `MMU_CHANGE_TOOL`.

### 4.3 Top-Level State Machine (extras)

```
IDLE
 │ [T0..TN / MMU_CHANGE_TOOL]
 ↓
UNLOADING ──[tip forming + RETRACT slave]──→ TIP_FORMING
 │
 ↓
SELECTING ──[FEED target slave]──→ LOADING
 │
 ↓ [master sensor triggered]
ASSIST_ACTIVE ──[RESUME extruder]──→ PRINTING
 │
 ↓ [runout / new T cmd]
UNLOADING (cycle repeats)

ERROR ←── any step on timeout/jam → PAUSE print + notify
```

### 4.4 Tool Change Sequence (full cycle)

```
1.  Klipper: MMU_CHANGE_TOOL TOOL=N
2.  extras: save current position, PAUSE extruder feed
3.  extras: execute tip-forming sequence (retract + wipe via Klipper extruder)
4.  extras → Master: rs485_send(current_slave, RETRACT, current_slot, speed)
5.  extras: poll GET_STATUS until slot_state[current_slot] == EMPTY (or timeout 30 s → ERROR)
6.  extras → Master: rs485_send(target_slave, FEED, target_slot, speed)
7.  extras: wait for master filament sensor triggered (endstop event, timeout 60 s → ERROR)
8.  extras → Master: rs485_send(target_slave, SET_ASSIST, target_slot, current_ma=150)
9.  extras: RESUME extruder feed
10. On error at any step: PAUSE print + RESPOND MSG with error description
```

### 4.5 Runout Detection

Two runout sources:
1. **Active** — Klipper G-code `T0..TN` (slicer-driven change)
2. **Passive** — slave detects empty slot (`EMPTY`) on the next `GET_STATUS` → extras automatically calls `MMU_CHANGE_TOOL` for the next available slot, or `PAUSE` if none are available

### 4.6 Configuration (printer.cfg)

```ini
[pico_mmu]
serial: /dev/serial/by-id/usb-Klipper_rp2350_XXXXX
rs485_baud: 115200
tool_count: 8
polling_interval: 50   # ms, ~20 Hz

[pico_mmu_slave box_0]
unique_id: 0x0123456789ABCDEF
slots: 0,1,2,3

[pico_mmu_slave box_1]
unique_id: 0xFEDCBA9876543210
slots: 4,5,6,7

# Standard T-commands
[gcode_macro T0]
gcode: MMU_CHANGE_TOOL TOOL=0

[gcode_macro T1]
gcode: MMU_CHANGE_TOOL TOOL=1
# ... and so on
```

---

## 5. Auto-Enumeration & Addressing

### 5.1 Startup Sequence

```
1. Master MCU boot → initialise RS485 (config_rs485)
2. Klipper connects → extras module loads
3. extras: call MMU_HOME (automatically or on command)
4. extras → Master: broadcast DISCOVER (addr=0xFF, several cycles)
5. Master: receives responses with unique_id, forwards to host via rs485_response
6. extras: matches unique_id with [pico_mmu_slave] in printer.cfg → gets target address
7. extras → Master: ASSIGN_ADDR(unique_id, assigned_addr) for each found slave
8. extras: GET_STATUS for each assigned slave → builds slot map
9. extras: logs result to Klipper console
```

### 5.2 Address Storage on Slave

- Address stored in flash (last 4 KB sector, via `rp2_flash` MicroPython API)
- On first boot (flash empty) — address = `0xFF` (unassigned)
- After `ASSIGN_ADDR` — address is saved and survives power cycles

---

## 6. Verification

| Test | Type | Pass Criteria |
|---|---|---|
| Frame encode/decode + CRC16 | Unit (Python) | All test vectors match on both ends |
| Slave state machine | Unit (mock hardware) | All EMPTY→LOADED→…→ERROR→LOADED transitions correct |
| RS485 loopback | Hardware (2× Pico 2) | All commands (0x01–0x0D) received without errors, latency < 10 ms |
| Auto-enumeration | Hardware (3 slaves) | All slaves discovered and assigned an address in 1 MMU_HOME cycle |
| Full tool change T0→T1 | Integration (Klipper) | Change completes in < 10 s, no errors, assist mode active |
| Stress polling | Hardware (8 slaves, 1 hour) | Packet loss < 0.1%, no hangs |

---

## 7. Design Decisions

| Decision | Rationale |
|---|---|
| Master in C (Klipper MCU firmware) | Native Klipper integration, standard filament sensor, no latency overhead |
| Slave in MicroPython | Fast iteration, PIO for real-time tasks, adequate performance |
| Auto-enumeration via RP2350 unique_id | No DIP switches, address persisted in flash |
| 20 Hz polling | Balance between responsiveness (50 ms) and bus load |
| Tip forming on the Klipper side | Full extruder control, reuse of existing logic |
| StallGuard for jam detection | No additional sensors, built-in TMC2209 capability |
| 5 s watchdog on slave | Safe stop on connection loss |

---

## 8. Out of Scope (Future Phases)

- OTA firmware updates to slaves over RS485
- Web UI / Mainsail integration
- Moonraker plugin
- Nema 17 support with different currents (config already accommodates this)
- Colour-coded spool presets (LED preset by filament type)
