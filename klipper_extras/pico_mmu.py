"""
Pico-MMU Klipper Extras Module
Orchestrates the distributed filament feeding system via Master MCU RS485 bridge.
"""

import json
import struct
from enum import IntEnum

# Klipper extras module conventions
HINT_FILAMENT_CHANGE = "MMU"


class PicoMmuError(Exception):
    pass


# --- Protocol constants (mirror of slave/bus/protocol.py) ---
class Cmd(IntEnum):
    DISCOVER = 0x01
    ASSIGN_ADDR = 0x02
    PING = 0x03
    GET_CONFIG = 0x04
    GET_STATUS = 0x05
    SET_FILAMENT = 0x06
    SET_CURRENT = 0x07
    HOME_SLOT = 0x08
    FEED = 0x09
    SET_ASSIST = 0x0A
    RETRACT = 0x0B
    STOP = 0x0C
    STOP_ALL = 0x0D


class Status(IntEnum):
    OK = 0x00
    BUSY = 0x01
    ERR_SLOT_EMPTY = 0x02
    ERR_JAM = 0x03
    ERR_TIMEOUT = 0x04


class SlotState(IntEnum):
    EMPTY = 0x00
    LOADED = 0x01
    FEEDING = 0x02
    RETRACTING = 0x03
    ASSIST = 0x04
    ERROR = 0x05


SLOT_STATE_NAMES = {
    SlotState.EMPTY: "EMPTY",
    SlotState.LOADED: "LOADED",
    SlotState.FEEDING: "FEEDING",
    SlotState.RETRACTING: "RETRACTING",
    SlotState.ASSIST: "ASSIST",
    SlotState.ERROR: "ERROR",
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
            config.get("unique_id", "0000000000000000").replace("0x", "")
        )
        self.slots = [int(s.strip()) for s in config.get("slots", "0,1,2,3").split(",")]
        # Per-slot filament colors: comma-separated hex values, one per slot
        default_colors = ",".join(["00FF00"] * len(self.slots))
        color_strs = [c.strip() for c in config.get("colors", default_colors).split(",")]
        if len(color_strs) != len(self.slots):
            raise config.error(
                "pico_mmu_slave 'colors' must have %d entries, got %d"
                % (len(self.slots), len(color_strs))
            )
        self.slot_colors = []
        for cs in color_strs:
            cs = cs.lstrip("#")
            if len(cs) != 6:
                raise config.error(
                    "pico_mmu_slave: invalid color '%s', must be 6 hex characters" % cs
                )
            try:
                self.slot_colors.append((int(cs[0:2], 16), int(cs[2:4], 16), int(cs[4:6], 16)))
            except ValueError:
                raise config.error("pico_mmu_slave: invalid hex color '%s'" % cs)
        self.addr = 0  # Assigned during enumeration
        self.online = False
        self.slot_states = [SlotState.EMPTY] * len(self.slots)
        self.fw_version = (0, 0)


class PicoMmu:
    """
    Main Klipper extras module for Pico-MMU.
    Manages tool changes, slave enumeration, and RS485 communication.
    """

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")

        # Configuration
        self.serial = config.get("serial")
        self.baud = config.getint("rs485_baud", 115200)
        self.tool_count = config.getint("tool_count", 8)
        self.polling_interval = config.getfloat("polling_interval", 0.05)

        # Load slave configurations
        self.slaves = []
        for i in range(16):  # Max 16 slave boxes
            section = f"pico_mmu_slave box_{i}"
            if config.has_section(section):
                slave_config = config.getsection(section)
                self.slaves.append(PicoMmuSlave(slave_config))

        # Build per-tool filament map (global tool number -> (r, g, b, material))
        # Defaults come from printer.cfg; saved overrides are loaded below
        self._tool_filaments = {}
        for slave in self.slaves:
            for global_slot, color in zip(slave.slots, slave.slot_colors):
                r, g, b = color
                self._tool_filaments[global_slot] = (r, g, b, "")

        # Load persisted filament overrides from [save_variables]
        svars = self.printer.lookup_object("save_variables", None)
        if svars is not None:
            saved = svars.get_status(None)["variables"].get("mmu_tool_filaments", {})
            for k, v in saved.items():
                try:
                    mat = str(v[3]) if len(v) > 3 else ""
                    self._tool_filaments[int(k)] = (int(v[0]), int(v[1]), int(v[2]), mat)
                except (ValueError, IndexError, TypeError):
                    pass

        # Infinite spool: per-tool group IDs (0 = ungrouped).
        # Format in printer.cfg:  slot_groups: 1,0,1,0
        # One comma-separated integer per global tool, indexed from 0.
        # Group 0 is the sentinel for "not in any group".
        # Tools with the same non-zero ID switch to each other on runout.
        # Omitted or all-zero means the feature is disabled.
        raw_groups = config.get("slot_groups", "")
        self._slot_groups: dict = {}
        if raw_groups.strip():
            for i, part in enumerate(raw_groups.split(",")):
                part = part.strip()
                if not part:
                    continue
                try:
                    self._slot_groups[i] = int(part)
                except ValueError:
                    raise config.error(
                        "pico_mmu: slot_groups entry %d is not an integer: '%s'" % (i, part)
                    )
        # Effective group map built by _build_slot_groups() after enumeration.
        self._effective_groups: dict = {}
        # Prevents scheduling more than one runout callback per poll cycle.
        self._runout_scheduled = False

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
        self.gcode.register_command(
            "MMU_HOME", self.cmd_MMU_HOME, desc="Initialize and enumerate MMU slaves"
        )
        self.gcode.register_command("MMU_STATUS", self.cmd_MMU_STATUS, desc="Display MMU status")
        self.gcode.register_command(
            "MMU_CHANGE_TOOL", self.cmd_MMU_CHANGE_TOOL, desc="Change filament tool"
        )
        self.gcode.register_command("MMU_LOAD", self.cmd_MMU_LOAD, desc="Load filament from slot")
        self.gcode.register_command(
            "MMU_UNLOAD", self.cmd_MMU_UNLOAD, desc="Unload current filament"
        )
        self.gcode.register_command(
            "MMU_SELECT", self.cmd_MMU_SELECT, desc="Select tool without loading"
        )
        self.gcode.register_command(
            "MMU_SET_FILAMENT",
            self.cmd_MMU_SET_FILAMENT,
            desc="Set filament color and material for a tool slot",
        )

    def _handle_connect(self):
        """Called when Klipper connects to MCU."""
        self.mcu = self.printer.lookup_object("mcu " + self.serial)

        # Register MCU commands
        self.rs485_cmd = self.mcu.lookup_command("pmu_rs485_send addr=%c cmd=%c data=%*s")
        self.rs485_query_cmd = self.mcu.lookup_query_command(
            "pmu_rs485_query addr=%c cmd=%c data=%*s timeout=%u",
            "pmu_rs485_rx addr=%c cmd=%c seq=%c data=%*s",
            is_async=True,
        )

        # Configure RS485 bus
        self.mcu.lookup_command(
            "config_pmu_rs485 uart_bus=%c tx_line=%u rx_line=%u de_line=%u baud=%u"
        ).send([1, 0, 1, 2, self.baud])  # UART1, pins from config

        # Start polling timer
        self._poll_timer = self.reactor.register_timer(self._poll_slaves, self.reactor.NOW)

    def _handle_disconnect(self):
        """Called on Klipper disconnect."""
        if self._poll_timer:
            self.reactor.unregister_timer(self._poll_timer)
            self._poll_timer = None

    # --- RS485 Communication ---

    def _rs485_send(self, addr, cmd, data=b""):
        """Send an RS485 frame (fire-and-forget)."""
        self.rs485_cmd.send([addr, cmd, data])

    def _rs485_query(self, addr, cmd, data=b"", timeout_ms=50):
        """Send an RS485 frame and wait for response."""
        timeout_ticks = self.mcu.seconds_to_clock(timeout_ms / 1000.0)
        params = self.rs485_query_cmd.send([addr, cmd, data, timeout_ticks])
        if params is None:
            return None
        return params.get("data", b"")

    # --- Polling ---

    def _poll_slaves(self, eventtime):
        """Periodic polling of all online slaves."""
        for slave in self.slaves:
            if not slave.online or slave.addr == 0:
                continue
            resp = self._rs485_query(slave.addr, Cmd.GET_STATUS)
            if resp and len(resp) >= 6:
                new_states = list(resp[:4])
                old_states = list(slave.slot_states)
                # Detect ASSIST→EMPTY on the currently printing slot.
                if self.state == STATE_PRINTING and self.current_tool >= 0:
                    ct_slave, ct_local = self._resolve_tool(self.current_tool)
                    if (
                        ct_slave is slave
                        and ct_local < len(old_states)
                        and old_states[ct_local] == SlotState.ASSIST
                        and new_states[ct_local] == SlotState.EMPTY
                        and not self._runout_scheduled
                    ):
                        self._runout_scheduled = True
                        self.reactor.register_callback(self._handle_runout)
                slave.slot_states = new_states
            else:
                slave.online = False

        return eventtime + self.polling_interval

    # --- Enumeration ---

    def _enumerate_slaves(self):
        """Auto-enumerate all slaves on the RS485 bus."""
        discovered = {}

        # Send DISCOVER broadcasts (multiple cycles)
        for cycle in range(5):
            resp = self._rs485_query(0xFF, Cmd.DISCOVER, timeout_ms=100)
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
                resp = self._rs485_query(0xFF, Cmd.ASSIGN_ADDR, addr_data)
                if resp and resp[0] == Status.OK:
                    slave.addr = next_addr
                    slave.online = True
                    self.gcode.respond_info(
                        f"MMU: Slave {slave.unique_id.hex()} assigned addr {next_addr}"
                    )
                    # Push stored filament info to the slave
                    for j, global_slot in enumerate(slave.slots):
                        if global_slot in self._tool_filaments:
                            r, g, b, mat = self._tool_filaments[global_slot]
                            mat_bytes = mat[:4].encode("ascii", errors="replace").ljust(4, b"\x00")
                            self._rs485_send(
                                slave.addr, Cmd.SET_FILAMENT, bytes([j, r, g, b]) + mat_bytes
                            )
                next_addr += 1
        self._build_slot_groups()

    # --- Infinite spool: group resolution ---

    def _build_slot_groups(self):
        """
        Build the effective per-tool group map from two sources:

        1. Explicit ``slot_groups`` config (highest priority).
           An explicit entry of 0 marks a tool as intentionally ungrouped,
           even if its color would otherwise create an auto-group.
        2. Auto-color matching: tools that share the same filament color and
           are *not* explicitly assigned a group are grouped automatically
           when two or more tools share that color.

        Stores the result in ``self._effective_groups``
        (``dict[tool_num -> group_id]``; only non-zero entries are stored).
        """
        explicit = self._slot_groups
        max_explicit = max(explicit.values(), default=0) if explicit else 0

        # Collect ungrouped tools per (color, material) key.
        color_tools: dict = {}
        for tool, filament in self._tool_filaments.items():
            if explicit.get(tool, 0) == 0:  # not explicitly assigned
                key = filament  # (r, g, b, material) — full identity
                if key not in color_tools:
                    color_tools[key] = []
                color_tools[key].append(tool)

        # Assign synthetic group IDs to colors shared by ≥2 ungrouped tools.
        auto: dict = {}
        next_gid = max_explicit + 1
        for color, tools in color_tools.items():
            if len(tools) >= 2:
                for t in tools:
                    auto[t] = next_gid
                next_gid += 1

        # Merge: explicit entries override auto (including explicit 0 = ungrouped).
        merged: dict = dict(auto)
        for tool, gid in explicit.items():
            merged[tool] = gid

        # Drop explicit-zero entries; 0 is the sentinel for "no group".
        self._effective_groups = {t: g for t, g in merged.items() if g != 0}

    def _get_group(self, tool):
        """Return the effective group ID for *tool*, or None if ungrouped."""
        gid = self._effective_groups.get(tool, 0)
        return gid if gid else None

    def _find_backup_tool(self, current_tool):
        """
        Find the first LOADED tool in the same infinite-spool group as
        *current_tool*, or return None if no backup is available.
        """
        gid = self._get_group(current_tool)
        if gid is None:
            return None
        for tool, g in self._effective_groups.items():
            if tool == current_tool or g != gid:
                continue
            slave, local_slot = self._resolve_tool(tool)
            if slave is None or not slave.online:
                continue
            if slave.slot_states[local_slot] == SlotState.LOADED:
                return tool
        return None

    # --- Infinite spool: runout handler ---

    def _handle_runout(self, eventtime):
        """
        Reactor callback fired when the active slot transitions ASSIST→EMPTY
        during printing.  Finds a backup slot in the same group and switches
        to it automatically, or pauses the print if none is available.
        """
        self._runout_scheduled = False
        if self.state != STATE_PRINTING or self.current_tool < 0:
            return
        backup = self._find_backup_tool(self.current_tool)
        if backup is None:
            self.gcode.respond_info(
                "MMU: T%d exhausted — no loaded backup in group. Pausing print." % self.current_tool
            )
            self.state = STATE_ERROR
            self.gcode.run_script_from_command("PAUSE")
            return
        self.gcode.respond_info(
            "MMU: T%d exhausted — switching to T%d (infinite spool)" % (self.current_tool, backup)
        )
        self._do_switch_to_backup(backup)

    def _do_switch_to_backup(self, new_tool):
        """
        Perform an automatic infinite-spool switch to *new_tool*.

        The exhausted slot is already EMPTY, so we skip the RETRACT command
        (which would return ERR_SLOT_EMPTY) and go straight to tip-forming
        followed by a normal load of the backup slot.
        """
        old_tool = self.current_tool
        try:
            # Tip-forming only (no RETRACT — filament already retracted by runout).
            self.state = STATE_TIP_FORMING
            self.gcode.run_script_from_command("PAUSE")
            self.gcode.run_script_from_command("G92 E0")
            self.gcode.run_script_from_command("G1 E-5 F3600")
            self.gcode.run_script_from_command("G1 E2 F1800")
            self.gcode.run_script_from_command("G1 E-15 F3000")
            self.gcode.run_script_from_command("G92 E0")

            slave, local_slot = self._resolve_tool(new_tool)
            # gcmd is None — _do_load does not use it directly.
            self._do_load(None, slave, local_slot, new_tool)

            self.current_tool = new_tool
            self.state = STATE_PRINTING
            self.gcode.run_script_from_command("RESUME")
            self.gcode.respond_info(
                "MMU: Infinite spool — switched from T%d to T%d" % (old_tool, new_tool)
            )
        except PicoMmuError as e:
            self.state = STATE_ERROR
            self._emergency_stop()
            self.gcode.respond_info("MMU: Infinite spool switch failed: %s" % e)

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
                resp = self._rs485_query(slave.addr, Cmd.GET_STATUS)
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
        tool = gcmd.get_int("TOOL")
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
        tool = gcmd.get_int("TOOL")
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
        tool = gcmd.get_int("TOOL")
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
        self.gcode.run_script_from_command("G1 E2 F1800")  # Pause
        self.gcode.run_script_from_command("G1 E-15 F3000")  # Long retract
        self.gcode.run_script_from_command("G92 E0")

        self.state = STATE_UNLOADING

        # Send retract to slave
        slave, local_slot = self._resolve_tool(self.current_tool)
        if slave is None:
            raise PicoMmuError("Current tool slave not found")

        speed_data = struct.pack("<BH", local_slot, 1000)
        resp = self._rs485_query(slave.addr, Cmd.RETRACT, speed_data)
        if resp is None or resp[0] != Status.OK:
            raise PicoMmuError(f"Retract command failed for T{self.current_tool}")

        # Wait for retract to complete (sensor clears)
        if not self._wait_slot_state(slave, local_slot, SlotState.EMPTY, timeout_s=30):
            raise PicoMmuError(f"Retract timeout for T{self.current_tool}")

    def _do_load(self, gcmd, slave, local_slot, tool):
        """Perform load sequence: feed + wait for master sensor + assist."""
        self.state = STATE_SELECTING

        # Send feed command to slave
        speed_data = struct.pack("<BH", local_slot, 800)
        resp = self._rs485_query(slave.addr, Cmd.FEED, speed_data)
        if resp is None or resp[0] != Status.OK:
            raise PicoMmuError(f"Feed command failed for T{tool}")

        self.state = STATE_LOADING

        # Wait for master filament sensor to trigger
        if not self._wait_master_sensor(timeout_s=60):
            # Stop the feed
            self._rs485_send(slave.addr, Cmd.STOP, bytes([local_slot]))
            raise PicoMmuError(f"Load timeout for T{tool} (filament didn't reach sensor)")

        # Switch to assist mode
        assist_data = struct.pack("<BH", local_slot, 150)  # 150 mA
        resp = self._rs485_query(slave.addr, Cmd.SET_ASSIST, assist_data)
        if resp is None or resp[0] != Status.OK:
            raise PicoMmuError(f"Assist mode failed for T{tool}")

        # Feed into extruder
        self.gcode.run_script_from_command("G92 E0")
        self.gcode.run_script_from_command("G1 E30 F300")  # Slow feed into hotend
        self.gcode.run_script_from_command("G92 E0")

    def _wait_slot_state(self, slave, local_slot, target_state, timeout_s=30):
        """Poll slave until slot reaches target state or timeout."""
        endtime = self.reactor.monotonic() + timeout_s
        while self.reactor.monotonic() < endtime:
            resp = self._rs485_query(slave.addr, Cmd.GET_STATUS)
            if resp and len(resp) >= 4:
                if resp[local_slot] == target_state:
                    return True
                if resp[local_slot] == SlotState.ERROR:
                    return False
            self.reactor.pause(self.reactor.monotonic() + 0.1)
        return False

    def _wait_master_sensor(self, timeout_s=60):
        """
        Wait for the master's filament sensor (endstop) to trigger.
        Uses Klipper's filament_switch_sensor status interface.
        """
        endtime = self.reactor.monotonic() + timeout_s
        try:
            sensor = self.printer.lookup_object("filament_switch_sensor mmu_sensor")
            while self.reactor.monotonic() < endtime:
                eventtime = self.reactor.monotonic()
                if sensor.get_status(eventtime)["filament_detected"]:
                    return True
                self.reactor.pause(self.reactor.monotonic() + 0.05)
        except Exception:
            # Sensor not configured — wait a fixed time as fallback
            self.reactor.pause(self.reactor.monotonic() + 5.0)
            return True
        return False

    def _emergency_stop(self):
        """Send emergency stop to all slaves."""
        for slave in self.slaves:
            if slave.online:
                self._rs485_send(slave.addr, Cmd.STOP_ALL)

    # --- Filament color ---

    def _parse_hex_color(self, hex_str):
        """Parse a hex color string (with or without #) to (r, g, b)."""
        s = hex_str.lstrip("#").strip()
        if len(s) != 6:
            raise ValueError("Color must be 6 hex characters, got '%s'" % hex_str)
        try:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
        except ValueError:
            raise ValueError("Invalid hex color '%s'" % hex_str)

    def cmd_MMU_SET_FILAMENT(self, gcmd):
        """MMU_SET_FILAMENT TOOL=N COLOR=RRGGBB [MATERIAL=name]: Set filament for a slot."""
        tool = gcmd.get_int("TOOL")
        color_str = gcmd.get("COLOR")
        material = gcmd.get("MATERIAL", "")[:4]
        if tool < 0 or tool >= self.tool_count:
            raise gcmd.error("MMU: Invalid tool number T%d" % tool)
        try:
            r, g, b = self._parse_hex_color(color_str)
        except ValueError as e:
            raise gcmd.error("MMU: %s" % e)
        slave, local_slot = self._resolve_tool(tool)
        if slave is None:
            raise gcmd.error("MMU: Tool T%d not configured" % tool)
        if not slave.online:
            raise gcmd.error("MMU: Slave for T%d is offline" % tool)
        self._tool_filaments[tool] = (r, g, b, material)
        self._build_slot_groups()
        mat_bytes = material.encode("ascii", errors="replace").ljust(4, b"\x00")
        self._rs485_send(slave.addr, Cmd.SET_FILAMENT, bytes([local_slot, r, g, b]) + mat_bytes)
        # Persist overrides via [save_variables]
        svars = self.printer.lookup_object("save_variables", None)
        if svars is not None:
            saved = dict(svars.get_status(None)["variables"].get("mmu_tool_filaments", {}))
            saved[str(tool)] = [r, g, b, material]
            self.gcode.run_script_from_command(
                "SAVE_VARIABLE VARIABLE=mmu_tool_filaments VALUE='%s'"
                % json.dumps(saved).replace("'", '"')
            )
        canon = color_str.lstrip("#").upper()
        gcmd.respond_info("MMU: Tool T%d set to #%s material=%s" % (tool, canon, material or "?"))


def load_config(config):
    """Klipper module entry point."""
    return PicoMmu(config)
