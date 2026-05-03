# Slave Firmware Configuration
# Pin assignments for RP2350 + TMC2209 × 4

# --- RS485 Bus ---
RS485_UART_ID = 0
RS485_TX_PIN = 0
RS485_RX_PIN = 1
RS485_DE_PIN = 2  # Direction Enable (HIGH = TX, LOW = RX)
RS485_BAUD = 115200

# --- TMC2209 UART (shared single-wire UART, addressed by MS pins) ---
TMC_UART_ID = 1
TMC_UART_TX_PIN = 4
TMC_UART_RX_PIN = 5

# --- Stepper Motors ---
STEP_PINS = [6, 8, 10, 12]
DIR_PINS = [7, 9, 11, 13]
EN_PINS = [14, 15, 16, 17]

NUM_SLOTS = len(STEP_PINS)  # Derived from pin config; change pin lists to resize

# TMC2209 UART addresses (set via MS1/MS2 pins on each driver)
TMC_ADDRESSES = [0, 1, 2, 3]

# --- Sensors (microswitch per slot) ---
SENSOR_PINS = [18, 19, 20, 21]
SENSOR_DEBOUNCE_MS = 5

# --- WS2812B LED Strip ---
LED_PIN = 22

# --- Motor Defaults ---
DEFAULT_RUN_CURRENT_MA = 800
DEFAULT_HOLD_CURRENT_MA = 200
DEFAULT_ASSIST_CURRENT_MA = 150
DEFAULT_FEED_SPEED_HZ = 800  # steps/sec
DEFAULT_RETRACT_SPEED_HZ = 1000

# --- Timeouts ---
FEED_TIMEOUT_MS = 60000  # 60 sec max feed time
RETRACT_TIMEOUT_MS = 30000  # 30 sec max retract time
WATCHDOG_TIMEOUT_MS = 5000  # 5 sec no-poll → emergency stop

# --- StallGuard ---
STALLGUARD_THRESHOLD = 20  # SG_RESULT below this = jam
STALLGUARD_POLL_MS = 200

# --- Address ---
DEVICE_ADDR_UNASSIGNED = 0xFF
FLASH_ADDR_OFFSET = 0  # sector offset in NVS for stored address

# --- Protocol ---
PREAMBLE = 0xAA
MAX_DATA_LEN = 64
BROADCAST_ADDR = 0x00
