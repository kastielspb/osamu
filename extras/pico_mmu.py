"""
Pico-MMU Klipper Extras Module
Orchestrates the distributed filament feeding system via Master MCU RS485 bridge.
"""

import logging
import struct

# Klipper extras module conventions
HINT_FILAMENT_CHANGE = "MMU"


class PicoMmuError(Exception):
    pass


# --- Protocol constants (mirror of slave/bus/protocol.py) ---
CMD_PING = 0x01
CMD_DISCOVER = 0x02
CMD_ASSIGN_ADDR = 0x03
CMD_GET_STATUS = 0x04
CMD_FEED = 0x05
CMD_RETRACT = 0x06
CMD_SET_ASSIST = 0x07
CMD_STOP = 0x08
CMD_STOP_ALL = 0x09
CMD_SET_LED = 0x0A
CMD_GET_CONFIG = 0x0B
CMD_SET_CURRENT = 0x0C
CMD_HOME_SLOT = 0x0D

STATUS_OK = 0x00
STATUS_BUSY = 0x01
STATUS_ERR_SLOT_EMPTY = 0x02
STATUS_ERR_JAM = 0x03
STATUS_ERR_TIMEOUT = 0x04

SLOT_EMPTY = 0x00
SLOT_LOADED = 0x01
SLOT_FEEDING = 0x02
SLOT_RETRACTING = 0x03
SLOT_ASSIST = 0x04
SLOT_ERROR = 0x05

SLOT_STATE_NAMES = {
    SLOT_EMPTY: "EMPTY",
    SLOT_LOADED: "LOADED",
    SLOT_FEEDING: "FEEDING",
    SLOT_RETRACTING: "RETRACTING",
    SLOT_ASSIST: "ASSIST",
    SLOT_ERROR: "ERROR",
}

# Top-level MMU states
STATE_IDLE = "idle"
STATE_UNLOADING = "unloading"
STATE_TIP_FORMING = "tip_forming"
STATE_SELECTING = "selecting"
STATE_LOADING = "loading"
STATE_PRINTING = "printing"
STATE_ERROR = "error"


class PicoMmuSlave:
    """Represents a single slave box on the RS485 bus."""

    def __init__(self, config):
        self.unique_id = bytes.fromhex(
            config.get('unique_id', '0000000000000000').replace('0x', ''))
        self.slots = [int(s.strip()) for s in config.get('slots', '0,1,2,3').split(',')]
        self.addr = 0  # Assigned during enumeration
        self.online = False
        self.slot_states = [SLOT_EMPTY] * 4
        self.fw_version = (0, 0)


class PicoMmu:
    """
    Main Klipper extras module for Pico-MMU.
    Manages tool changes, slave enumeration, and RS485 communication.
    """

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        # Configuration
        self.serial = config.get('serial')
        self.baud = config.getint('rs485_baud', 115200)
        self.tool_count = config.getint('tool_count', 8)
        self.polling_interval = config.getfloat('polling_interval', 0.05)

        # Load slave configurations
        self.slaves = []
        printer_config = config.get_printer().lookup_object('configfile')
        for i in range(16):  # Max 16 slave boxes
            section = f'pico_mmu_slave box_{i}'
            if config.has_section(section):
                slave_config = config.getsection(section)
                self.slaves.append(PicoMmuSlave(slave_config))

        # State
        self.state = STATE_IDLE
        self.current_tool = -1  # No tool loaded
        self.target_tool = -1
        self.mcu = None
        self.rs485_cmd = None
        self.rs485_query_cmd = None

        # Polling timer
        self._poll_timer = None

        # Register event handlers
        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("klippy:disconnect", self._handle_disconnect)

        # Register G-code commands
        self.gcode.register_command('MMU_HOME', self.cmd_MMU_HOME,
                                    desc="Initialize and enumerate MMU slaves")
        self.gcode.register_command('MMU_STATUS', self.cmd_MMU_STATUS,
                                    desc="Display MMU status")
        self.gcode.register_command('MMU_CHANGE_TOOL', self.cmd_MMU_CHANGE_TOOL,
                                    desc="Change filament tool")
        self.gcode.register_command('MMU_LOAD', self.cmd_MMU_LOAD,
                                    desc="Load filament from slot")
        self.gcode.register_command('MMU_UNLOAD', self.cmd_MMU_UNLOAD,
                                    desc="Unload current filament")
        self.gcode.register_command('MMU_SELECT', self.cmd_MMU_SELECT,
                                    desc="Select tool without loading")

    def _handle_connect(self):
        """Called when Klipper connects to MCU."""
        self.mcu = self.printer.lookup_object('mcu ' + self.serial)

        # Register MCU commands
        self.rs485_cmd = self.mcu.lookup_command(
            "pmu_rs485_send addr=%c cmd=%c data=%*s")
        self.rs485_query_cmd = self.mcu.lookup_query_command(
            "pmu_rs485_query addr=%c cmd=%c data=%*s timeout=%u",
            "pmu_rs485_rx addr=%c cmd=%c seq=%c data=%*s")

        # Configure RS485 bus
        self.mcu.lookup_command("config_pmu_rs485 uart_bus=%c tx_pin=%u "
                               "rx_pin=%u de_pin=%u baud=%u").send(
            [1, 0, 1, 2, self.baud])  # UART1, pins from config

        # Start polling timer
        self._poll_timer = self.reactor.register_timer(
            self._poll_slaves, self.reactor.NOW)

    def _handle_disconnect(self):
        """Called on Klipper disconnect."""
        if self._poll_timer:
            self.reactor.unregister_timer(self._poll_timer)
            self._poll_timer = None

    # --- RS485 Communication ---

    def _rs485_send(self, addr, cmd, data=b''):
        """Send an RS485 frame (fire-and-forget)."""
        self.rs485_cmd.send([addr, cmd, data])

    def _rs485_query(self, addr, cmd, data=b'', timeout_ms=50):
        """Send an RS485 frame and wait for response."""
        timeout_ticks = int(timeout_ms * self.mcu.get_adjusted_freq() / 1000)
        params = self.rs485_query_cmd.send([addr, cmd, data, timeout_ticks])
        if params is None:
            return None
        return params.get('data', b'')

    # --- Polling ---

    def _poll_slaves(self, eventtime):
        """Periodic polling of all online slaves."""
        for slave in self.slaves:
            if not slave.online or slave.addr == 0:
                continue
            resp = self._rs485_query(slave.addr, CMD_GET_STATUS)
            if resp and len(resp) >= 6:
                slave.slot_states = list(resp[:4])
            else:
                slave.online = False

        return eventtime + self.polling_interval

    # --- Enumeration ---

    def _enumerate_slaves(self):
        """Auto-enumerate all slaves on the RS485 bus."""
        discovered = {}

        # Send DISCOVER broadcasts (multiple cycles)
        for cycle in range(5):
            resp = self._rs485_query(0xFF, CMD_DISCOVER, timeout_ms=100)
            if resp and len(resp) >= 8:
                uid = resp[:8]
                discovered[uid] = True

        if not discovered:
            self.gcode.respond_info("MMU: No slaves discovered")
            return

        # Assign addresses based on config
        next_addr = 1
        for slave in self.slaves:
            if slave.unique_id in discovered:
                addr_data = slave.unique_id + bytes([next_addr])
                resp = self._rs485_query(0xFF, CMD_ASSIGN_ADDR, addr_data)
                if resp and resp[0] == STATUS_OK:
                    slave.addr = next_addr
                    slave.online = True
                    self.gcode.respond_info(
                        f"MMU: Slave {slave.unique_id.hex()} assigned addr {next_addr}")
                next_addr += 1

    # --- Tool resolution ---

    def _resolve_tool(self, tool_num):
        """
        Resolve global tool number to (slave, local_slot).
        Tools are numbered sequentially across all slaves.
        """
        for slave in self.slaves:
            if tool_num in slave.slots:
                local_slot = slave.slots.index(tool_num)
                return slave, local_slot
        return None, None

    # --- G-code commands ---

    def cmd_MMU_HOME(self, gcmd):
        """MMU_HOME: Enumerate and initialize all slaves."""
        self.gcode.respond_info("MMU: Starting enumeration...")
        self._enumerate_slaves()

        # Query initial status from all online slaves
        online_count = 0
        for slave in self.slaves:
            if slave.online:
                online_count += 1
                resp = self._rs485_query(slave.addr, CMD_GET_STATUS)
                if resp and len(resp) >= 4:
                    slave.slot_states = list(resp[:4])

        self.gcode.respond_info(f"MMU: Home complete. {online_count} slaves online.")
        self.state = STATE_IDLE

    def cmd_MMU_STATUS(self, gcmd):
        """MMU_STATUS: Display current MMU state."""
        lines = [f"MMU State: {self.state}, Current tool: T{self.current_tool}"]

        for i, slave in enumerate(self.slaves):
            status = "ONLINE" if slave.online else "OFFLINE"
            lines.append(f"  Box {i} [{status}] addr={slave.addr}:")
            for j, global_slot in enumerate(slave.slots):
                state_name = SLOT_STATE_NAMES.get(slave.slot_states[j], "UNKNOWN")
                lines.append(f"    T{global_slot}: {state_name}")

        self.gcode.respond_info("\n".join(lines))

    def cmd_MMU_CHANGE_TOOL(self, gcmd):
        """MMU_CHANGE_TOOL TOOL=N: Full tool change sequence."""
        tool = gcmd.get_int('TOOL')
        if tool < 0 or tool >= self.tool_count:
            raise gcmd.error(f"MMU: Invalid tool number T{tool}")

        slave, local_slot = self._resolve_tool(tool)
        if slave is None:
            raise gcmd.error(f"MMU: Tool T{tool} not configured")
        if not slave.online:
            raise gcmd.error(f"MMU: Slave for T{tool} is offline")

        self.target_tool = tool

        try:
            # 1. Unload current tool if loaded
            if self.current_tool >= 0:
                self._do_unload(gcmd)

            # 2. Load new tool
            self._do_load(gcmd, slave, local_slot, tool)

            self.current_tool = tool
            self.state = STATE_PRINTING

        except PicoMmuError as e:
            self.state = STATE_ERROR
            self._emergency_stop()
            # Pause print
            self.gcode.run_script_from_command("PAUSE")
            raise gcmd.error(f"MMU: Tool change failed: {e}")

    def cmd_MMU_LOAD(self, gcmd):
        """MMU_LOAD TOOL=N: Load filament without full change."""
        tool = gcmd.get_int('TOOL')
        slave, local_slot = self._resolve_tool(tool)
        if slave is None or not slave.online:
            raise gcmd.error(f"MMU: Tool T{tool} unavailable")

        try:
            self._do_load(gcmd, slave, local_slot, tool)
            self.current_tool = tool
        except PicoMmuError as e:
            self.state = STATE_ERROR
            raise gcmd.error(f"MMU: Load failed: {e}")

    def cmd_MMU_UNLOAD(self, gcmd):
        """MMU_UNLOAD: Unload current filament."""
        if self.current_tool < 0:
            self.gcode.respond_info("MMU: No tool loaded")
            return

        try:
            self._do_unload(gcmd)
            self.current_tool = -1
        except PicoMmuError as e:
            self.state = STATE_ERROR
            raise gcmd.error(f"MMU: Unload failed: {e}")

    def cmd_MMU_SELECT(self, gcmd):
        """MMU_SELECT TOOL=N: Select tool without loading."""
        tool = gcmd.get_int('TOOL')
        slave, local_slot = self._resolve_tool(tool)
        if slave is None:
            raise gcmd.error(f"MMU: Tool T{tool} not configured")
        self.target_tool = tool
        self.gcode.respond_info(f"MMU: Selected T{tool}")

    # --- Internal operations ---

    def _do_unload(self, gcmd):
        """Perform unload sequence: tip forming + retract."""
        self.state = STATE_TIP_FORMING

        # Tip forming (retract sequence via extruder)
        self.gcode.run_script_from_command("G92 E0")
        self.gcode.run_script_from_command("G1 E-5 F3600")  # Quick retract
        self.gcode.run_script_from_command("G1 E2 F1800")   # Pause
        self.gcode.run_script_from_command("G1 E-15 F3000") # Long retract
        self.gcode.run_script_from_command("G92 E0")

        self.state = STATE_UNLOADING

        # Send retract to slave
        slave, local_slot = self._resolve_tool(self.current_tool)
        if slave is None:
            raise PicoMmuError("Current tool slave not found")

        speed_data = struct.pack('<BH', local_slot, 1000)
        resp = self._rs485_query(slave.addr, CMD_RETRACT, speed_data)
        if resp is None or resp[0] != STATUS_OK:
            raise PicoMmuError(f"Retract command failed for T{self.current_tool}")

        # Wait for retract to complete (sensor clears)
        if not self._wait_slot_state(slave, local_slot, SLOT_EMPTY, timeout_s=30):
            raise PicoMmuError(f"Retract timeout for T{self.current_tool}")

    def _do_load(self, gcmd, slave, local_slot, tool):
        """Perform load sequence: feed + wait for master sensor + assist."""
        self.state = STATE_SELECTING

        # Send feed command to slave
        speed_data = struct.pack('<BH', local_slot, 800)
        resp = self._rs485_query(slave.addr, CMD_FEED, speed_data)
        if resp is None or resp[0] != STATUS_OK:
            raise PicoMmuError(f"Feed command failed for T{tool}")

        self.state = STATE_LOADING

        # Wait for master filament sensor to trigger
        if not self._wait_master_sensor(timeout_s=60):
            # Stop the feed
            self._rs485_send(slave.addr, CMD_STOP, bytes([local_slot]))
            raise PicoMmuError(f"Load timeout for T{tool} (filament didn't reach sensor)")

        # Switch to assist mode
        assist_data = struct.pack('<BH', local_slot, 150)  # 150 mA
        resp = self._rs485_query(slave.addr, CMD_SET_ASSIST, assist_data)
        if resp is None or resp[0] != STATUS_OK:
            raise PicoMmuError(f"Assist mode failed for T{tool}")

        # Feed into extruder
        self.gcode.run_script_from_command("G92 E0")
        self.gcode.run_script_from_command("G1 E30 F300")  # Slow feed into hotend
        self.gcode.run_script_from_command("G92 E0")

    def _wait_slot_state(self, slave, local_slot, target_state, timeout_s=30):
        """Poll slave until slot reaches target state or timeout."""
        endtime = self.reactor.monotonic() + timeout_s
        while self.reactor.monotonic() < endtime:
            resp = self._rs485_query(slave.addr, CMD_GET_STATUS)
            if resp and len(resp) >= 4:
                if resp[local_slot] == target_state:
                    return True
                if resp[local_slot] == SLOT_ERROR:
                    return False
            self.reactor.pause(self.reactor.monotonic() + 0.1)
        return False

    def _wait_master_sensor(self, timeout_s=60):
        """
        Wait for the master's filament sensor (endstop) to trigger.
        Uses Klipper's endstop query mechanism.
        """
        endtime = self.reactor.monotonic() + timeout_s
        # Get the filament sensor endstop object
        try:
            sensor = self.printer.lookup_object('filament_switch_sensor mmu_sensor')
            while self.reactor.monotonic() < endtime:
                if sensor.filament_present:
                    return True
                self.reactor.pause(self.reactor.monotonic() + 0.05)
        except Exception:
            # Fallback: just wait a reasonable time
            self.reactor.pause(self.reactor.monotonic() + 5.0)
            return True
        return False

    def _emergency_stop(self):
        """Send emergency stop to all slaves."""
        for slave in self.slaves:
            if slave.online:
                self._rs485_send(slave.addr, CMD_STOP_ALL)


def load_config(config):
    """Klipper module entry point."""
    return PicoMmu(config)
