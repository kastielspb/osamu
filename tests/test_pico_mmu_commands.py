"""
Unit tests for Klipper G-code command handlers in pico_mmu.py.

Exercises cmd_MMU_HOME, cmd_MMU_CHANGE_TOOL, cmd_MMU_UNLOAD, cmd_MMU_LOAD,
and cmd_MMU_SET_FILAMENT without a Klipper runtime or real RS485 hardware.
All RS485 I/O and reactor waits are mocked.

Scenarios:
  CMD-1  cmd_MMU_HOME — discovers slaves, pushes saved filament info
  CMD-2  cmd_MMU_HOME — slave not in config is not enrolled
  CMD-3  cmd_MMU_CHANGE_TOOL — full unload + load success
  CMD-4  cmd_MMU_CHANGE_TOOL — no current tool (first load)
  CMD-5  cmd_MMU_CHANGE_TOOL — RETRACT RS485 failure pauses print
  CMD-6  cmd_MMU_CHANGE_TOOL — invalid tool number raises error
  CMD-7  cmd_MMU_UNLOAD — no tool loaded is a no-op
  CMD-8  cmd_MMU_UNLOAD — normal unload flow
  CMD-9  cmd_MMU_LOAD — basic load
  CMD-10 cmd_MMU_SET_FILAMENT — stores info, sends RS485, persists variable
  CMD-11 cmd_MMU_SET_FILAMENT — invalid tool number raises error
  CMD-12 cmd_MMU_SET_FILAMENT — invalid hex color raises error
  CMD-13 cmd_MMU_SET_FILAMENT — offline slave raises error
"""

import importlib.util
import json
import os
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Import pico_mmu without a Klipper runtime.
# ---------------------------------------------------------------------------
_EXTRAS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "klipper_extras"))
_spec = importlib.util.spec_from_file_location("pico_mmu", os.path.join(_EXTRAS_DIR, "pico_mmu.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

PicoMmu = _mod.PicoMmu
PicoMmuSlave = _mod.PicoMmuSlave
PicoMmuError = _mod.PicoMmuError
SlotState = _mod.SlotState
Cmd = _mod.Cmd
Status = _mod.Status
STATE_IDLE = _mod.STATE_IDLE
STATE_PRINTING = _mod.STATE_PRINTING
STATE_UNLOADING = _mod.STATE_UNLOADING
STATE_ERROR = _mod.STATE_ERROR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UID_A = b"\x01\x02\x03\x04\x05\x06\x07\x08"
_UID_B = b"\xaa\xbb\xcc\xdd\xee\xff\x11\x22"


def _make_slave(slots, uid=_UID_A, online=False, addr=0):
    """Build a PicoMmuSlave without going through Klipper config."""
    s = PicoMmuSlave.__new__(PicoMmuSlave)
    s.unique_id = uid
    s.slots = list(slots)
    s.slot_colors = [(0, 255, 0)] * len(slots)
    s.addr = addr
    s.online = online
    s.slot_states = [SlotState.EMPTY] * len(slots)
    s.fw_version = (0, 1)
    return s


def _make_gcmd(tool=None, color=None, material=""):
    """Build a minimal mock gcmd object."""
    gcmd = MagicMock()
    gcmd.get_int.side_effect = lambda k, **kw: {
        "TOOL": tool,
    }.get(k, kw.get("default"))
    gcmd.get.side_effect = lambda k, default=None: {
        "COLOR": color,
        "MATERIAL": material,
    }.get(k, default)
    gcmd.error.side_effect = lambda msg: Exception(msg)
    return gcmd


def _make_mmu(
    *,
    slaves=None,
    tool_count=8,
    state=STATE_IDLE,
    current_tool=-1,
    tool_filaments=None,
    slot_groups=None,
):
    """
    Build a PicoMmu without Klipper __init__.
    RS485 methods and reactor.pause are set to benign mocks.
    """
    mmu = PicoMmu.__new__(PicoMmu)
    mmu.slaves = slaves if slaves is not None else []
    mmu.tool_count = tool_count
    mmu.state = state
    mmu.current_tool = current_tool
    mmu.target_tool = -1
    mmu._slot_groups = slot_groups or {}
    mmu._effective_groups = {}
    mmu._runout_scheduled = False
    mmu._tool_filaments = tool_filaments or {}
    mmu.polling_interval = 0.05

    mmu.gcode = MagicMock()
    mmu.reactor = MagicMock()
    mmu.reactor.monotonic.return_value = 0.0
    mmu.printer = MagicMock()

    mmu.mcu = MagicMock()
    mmu.rs485_cmd = MagicMock()
    mmu.rs485_query_cmd = MagicMock()

    # Default RS485 stubs (tests override as needed)
    mmu._rs485_send = MagicMock()
    mmu._rs485_query = MagicMock(return_value=bytes([Status.OK]))

    return mmu


# ---------------------------------------------------------------------------
# CMD-1: cmd_MMU_HOME — discovers slaves, assigns addresses, pushes filament
# ---------------------------------------------------------------------------


def test_home_enrolls_known_slave():
    """
    _enumerate_slaves finds a slave whose UID matches a configured entry.
    It must assign an address, mark the slave online, and push stored filament info.
    """
    slave = _make_slave(slots=[0, 1, 2, 3], uid=_UID_A)
    mmu = _make_mmu(
        slaves=[slave],
        tool_filaments={
            0: (255, 51, 0, "PLA"),
            1: (0, 255, 0, ""),
            2: (0, 0, 255, "ABS"),
            3: (255, 255, 0, ""),
        },
    )

    # DISCOVER returns UID A; ASSIGN_ADDR + GET_STATUS return OK / 6-byte status
    def _query_side(addr, cmd, data=b"", **kw):
        if cmd == Cmd.DISCOVER:
            return _UID_A
        if cmd == Cmd.ASSIGN_ADDR:
            return bytes([Status.OK])
        if cmd == Cmd.GET_STATUS:
            return bytes(6)
        return bytes([Status.OK])

    mmu._rs485_query = MagicMock(side_effect=_query_side)

    gcmd = MagicMock()
    mmu.cmd_MMU_HOME(gcmd)

    assert slave.online is True
    assert slave.addr == 1
    assert mmu.state == STATE_IDLE

    # Filament info for all 4 slots must have been sent
    set_fil_calls = [c for c in mmu._rs485_send.call_args_list if c[0][1] == Cmd.SET_FILAMENT]
    assert len(set_fil_calls) == 4, (
        f"Expected 4 SET_FILAMENT sends during home, got {len(set_fil_calls)}"
    )


# ---------------------------------------------------------------------------
# CMD-2: cmd_MMU_HOME — unknown UID is not enrolled
# ---------------------------------------------------------------------------


def test_home_ignores_unknown_uid():
    """A discovered UID not matching any configured slave must not be enrolled."""
    slave = _make_slave(slots=[0, 1, 2, 3], uid=_UID_A)
    mmu = _make_mmu(slaves=[slave])

    # DISCOVER returns an unknown UID
    unknown_uid = b"\xff\xfe\xfd\xfc\xfb\xfa\xf9\xf8"

    def _query_side(addr, cmd, data=b"", **kw):
        if cmd == Cmd.DISCOVER:
            return unknown_uid
        return bytes([Status.OK])

    mmu._rs485_query = MagicMock(side_effect=_query_side)

    gcmd = MagicMock()
    mmu.cmd_MMU_HOME(gcmd)

    assert slave.online is False, "Slave with non-matching UID must remain offline"
    assert slave.addr == 0


# ---------------------------------------------------------------------------
# CMD-3: cmd_MMU_CHANGE_TOOL — full unload + load success
# ---------------------------------------------------------------------------


def test_change_tool_unload_then_load():
    """
    With T0 currently loaded and T1 requested:
    - Tip forming G-code must run
    - RETRACT must be sent for slot 0
    - FEED + SET_ASSIST must be sent for slot 1
    - current_tool updated to 1, state → PRINTING
    """
    slave = _make_slave(slots=[0, 1, 2, 3], online=True, addr=1)
    slave.slot_states = [SlotState.LOADED, SlotState.LOADED, SlotState.EMPTY, SlotState.EMPTY]
    mmu = _make_mmu(slaves=[slave], state=STATE_PRINTING, current_tool=0)

    def _query_side(addr, cmd, data=b"", **kw):
        if cmd == Cmd.RETRACT:
            slave.slot_states[0] = SlotState.EMPTY
            return bytes([Status.OK])
        if cmd == Cmd.FEED:
            return bytes([Status.OK])
        if cmd == Cmd.SET_ASSIST:
            return bytes([Status.OK])
        if cmd == Cmd.GET_STATUS:
            return bytes(slave.slot_states[:4]) + bytes(2)
        return bytes([Status.OK])

    mmu._rs485_query = MagicMock(side_effect=_query_side)

    # _wait_slot_state and _wait_master_sensor must succeed immediately
    with (
        patch.object(mmu, "_wait_slot_state", return_value=True),
        patch.object(mmu, "_wait_master_sensor", return_value=True),
    ):
        gcmd = _make_gcmd(tool=1)
        mmu.cmd_MMU_CHANGE_TOOL(gcmd)

    assert mmu.current_tool == 1
    assert mmu.state == STATE_PRINTING

    # Tip-forming retract G-code must have fired
    gcode_calls = [c[0][0] for c in mmu.gcode.run_script_from_command.call_args_list]
    assert any("E-5" in s or "E-15" in s for s in gcode_calls), (
        "Tip-forming extruder retract G-code must run during unload"
    )

    # RETRACT for slot 0 must have been issued
    rs485_cmds = [c[0][1] for c in mmu._rs485_query.call_args_list]
    assert Cmd.RETRACT in rs485_cmds
    assert Cmd.FEED in rs485_cmds
    assert Cmd.SET_ASSIST in rs485_cmds


# ---------------------------------------------------------------------------
# CMD-4: cmd_MMU_CHANGE_TOOL — first load (no current tool)
# ---------------------------------------------------------------------------


def test_change_tool_no_current_tool_skips_unload():
    """When current_tool == -1 the unload phase is skipped entirely."""
    slave = _make_slave(slots=[0, 1, 2, 3], online=True, addr=1)
    slave.slot_states = [SlotState.LOADED] * 4
    mmu = _make_mmu(slaves=[slave], state=STATE_IDLE, current_tool=-1)

    mmu._rs485_query = MagicMock(return_value=bytes([Status.OK]))

    with (
        patch.object(mmu, "_wait_slot_state", return_value=True),
        patch.object(mmu, "_wait_master_sensor", return_value=True),
    ):
        gcmd = _make_gcmd(tool=0)
        mmu.cmd_MMU_CHANGE_TOOL(gcmd)

    assert mmu.current_tool == 0
    # No RETRACT should have been sent
    rs485_cmds = [c[0][1] for c in mmu._rs485_query.call_args_list]
    assert Cmd.RETRACT not in rs485_cmds, "No RETRACT when there is no current tool"


# ---------------------------------------------------------------------------
# CMD-5: cmd_MMU_CHANGE_TOOL — RETRACT failure pauses print and sets error
# ---------------------------------------------------------------------------


def test_change_tool_retract_failure_sets_error():
    """If RETRACT returns a failure status the MMU must pause and enter ERROR."""
    slave = _make_slave(slots=[0, 1, 2, 3], online=True, addr=1)
    slave.slot_states = [SlotState.LOADED] * 4
    mmu = _make_mmu(slaves=[slave], state=STATE_PRINTING, current_tool=0)

    mmu._rs485_query = MagicMock(return_value=bytes([Status.BUSY]))

    with patch.object(mmu, "_wait_slot_state", return_value=True):
        gcmd = _make_gcmd(tool=1)
        try:
            mmu.cmd_MMU_CHANGE_TOOL(gcmd)
        except Exception:
            pass  # gcmd.error raises — expected

    assert mmu.state == STATE_ERROR
    mmu.gcode.run_script_from_command.assert_any_call("PAUSE")


# ---------------------------------------------------------------------------
# CMD-6: cmd_MMU_CHANGE_TOOL — invalid tool number raises error
# ---------------------------------------------------------------------------


def test_change_tool_invalid_tool_raises():
    """Tool number ≥ tool_count must raise via gcmd.error."""
    mmu = _make_mmu(tool_count=4)
    gcmd = _make_gcmd(tool=99)

    try:
        mmu.cmd_MMU_CHANGE_TOOL(gcmd)
    except Exception:
        pass

    gcmd.error.assert_called_once()
    msg = gcmd.error.call_args[0][0]
    assert "T99" in msg or "invalid" in msg.lower()


# ---------------------------------------------------------------------------
# CMD-7: cmd_MMU_UNLOAD — no tool loaded is a no-op
# ---------------------------------------------------------------------------


def test_unload_no_tool_is_noop():
    """MMU_UNLOAD with current_tool == -1 must respond with info and not send RS485."""
    mmu = _make_mmu(current_tool=-1)
    gcmd = MagicMock()
    mmu.cmd_MMU_UNLOAD(gcmd)

    mmu._rs485_query.assert_not_called()
    mmu._rs485_send.assert_not_called()
    mmu.gcode.respond_info.assert_called_once()


# ---------------------------------------------------------------------------
# CMD-8: cmd_MMU_UNLOAD — normal unload flow
# ---------------------------------------------------------------------------


def test_unload_normal_flow():
    """MMU_UNLOAD retracts the current tool and sets current_tool to -1."""
    slave = _make_slave(slots=[0, 1, 2, 3], online=True, addr=1)
    slave.slot_states = [SlotState.LOADED, SlotState.EMPTY, SlotState.EMPTY, SlotState.EMPTY]
    mmu = _make_mmu(slaves=[slave], state=STATE_PRINTING, current_tool=0)

    mmu._rs485_query = MagicMock(return_value=bytes([Status.OK]))

    with patch.object(mmu, "_wait_slot_state", return_value=True):
        gcmd = MagicMock()
        mmu.cmd_MMU_UNLOAD(gcmd)

    assert mmu.current_tool == -1
    rs485_cmds = [c[0][1] for c in mmu._rs485_query.call_args_list]
    assert Cmd.RETRACT in rs485_cmds


# ---------------------------------------------------------------------------
# CMD-9: cmd_MMU_LOAD — basic load sets current_tool
# ---------------------------------------------------------------------------


def test_load_sets_current_tool():
    """MMU_LOAD TOOL=2 must load slot 2 and update current_tool."""
    slave = _make_slave(slots=[0, 1, 2, 3], online=True, addr=1)
    slave.slot_states = [SlotState.EMPTY, SlotState.EMPTY, SlotState.LOADED, SlotState.EMPTY]
    mmu = _make_mmu(slaves=[slave], state=STATE_IDLE, current_tool=-1)

    mmu._rs485_query = MagicMock(return_value=bytes([Status.OK]))

    with (
        patch.object(mmu, "_wait_master_sensor", return_value=True),
        patch.object(mmu, "_wait_slot_state", return_value=True),
    ):
        gcmd = _make_gcmd(tool=2)
        mmu.cmd_MMU_LOAD(gcmd)

    assert mmu.current_tool == 2


# ---------------------------------------------------------------------------
# CMD-10: cmd_MMU_SET_FILAMENT — stores info, sends RS485, persists variable
# ---------------------------------------------------------------------------


def test_set_filament_stores_and_persists():
    """
    MMU_SET_FILAMENT TOOL=1 COLOR=FF3300 MATERIAL=PLA must:
    - update _tool_filaments[1]
    - send SET_FILAMENT over RS485
    - invoke SAVE_VARIABLE via run_script_from_command
    """
    slave = _make_slave(slots=[0, 1, 2, 3], online=True, addr=1)
    mmu = _make_mmu(
        slaves=[slave],
        tool_filaments={
            0: (0, 255, 0, ""),
            1: (0, 255, 0, ""),
            2: (0, 255, 0, ""),
            3: (0, 255, 0, ""),
        },
    )

    # Provide a fake save_variables object
    fake_svars = MagicMock()
    fake_svars.get_status.return_value = {"variables": {"mmu_tool_filaments": {}}}
    mmu.printer.lookup_object.return_value = fake_svars

    gcmd = _make_gcmd(tool=1, color="FF3300", material="PLA")
    mmu.cmd_MMU_SET_FILAMENT(gcmd)

    assert mmu._tool_filaments[1] == (255, 51, 0, "PLA")

    # RS485 SET_FILAMENT sent with correct slot and color
    set_fil_calls = [c for c in mmu._rs485_send.call_args_list if c[0][1] == Cmd.SET_FILAMENT]
    assert len(set_fil_calls) == 1
    payload = set_fil_calls[0][0][2]
    assert payload[0] == 1  # local slot index
    assert payload[1] == 255  # R
    assert payload[2] == 51  # G
    assert payload[3] == 0  # B

    # SAVE_VARIABLE must have been called with the serialized dict
    save_calls = [
        c[0][0]
        for c in mmu.gcode.run_script_from_command.call_args_list
        if "SAVE_VARIABLE" in c[0][0]
    ]
    assert len(save_calls) == 1, "SAVE_VARIABLE must be called exactly once"
    assert "mmu_tool_filaments" in save_calls[0]
    # The JSON payload must include the tool entry
    json_part = save_calls[0].split("VALUE=")[1].strip("'")
    saved = json.loads(json_part)
    assert "1" in saved
    assert saved["1"][:3] == [255, 51, 0]
    assert saved["1"][3] == "PLA"


# ---------------------------------------------------------------------------
# CMD-11: cmd_MMU_SET_FILAMENT — invalid tool number
# ---------------------------------------------------------------------------


def test_set_filament_invalid_tool():
    """Tool number ≥ tool_count must raise via gcmd.error."""
    mmu = _make_mmu(tool_count=4)
    gcmd = _make_gcmd(tool=99, color="FF0000")

    try:
        mmu.cmd_MMU_SET_FILAMENT(gcmd)
    except Exception:
        pass

    gcmd.error.assert_called_once()


# ---------------------------------------------------------------------------
# CMD-12: cmd_MMU_SET_FILAMENT — invalid hex color
# ---------------------------------------------------------------------------


def test_set_filament_invalid_color():
    """A non-hex or wrong-length color string must raise via gcmd.error."""
    slave = _make_slave(slots=[0, 1, 2, 3], online=True, addr=1)
    mmu = _make_mmu(slaves=[slave], tool_count=4)

    for bad_color in ("ZZZZZZ", "FF33", "12345", ""):
        gcmd = _make_gcmd(tool=0, color=bad_color)
        try:
            mmu.cmd_MMU_SET_FILAMENT(gcmd)
        except Exception:
            pass
        gcmd.error.assert_called()


# ---------------------------------------------------------------------------
# CMD-13: cmd_MMU_SET_FILAMENT — offline slave
# ---------------------------------------------------------------------------


def test_set_filament_offline_slave_raises():
    """If the slave hosting the requested tool is offline, gcmd.error must be raised."""
    slave = _make_slave(slots=[0, 1, 2, 3], online=False, addr=0)
    mmu = _make_mmu(slaves=[slave], tool_count=4)

    gcmd = _make_gcmd(tool=0, color="00FF00", material="PLA")
    try:
        mmu.cmd_MMU_SET_FILAMENT(gcmd)
    except Exception:
        pass

    gcmd.error.assert_called_once()
    msg = gcmd.error.call_args[0][0]
    assert "offline" in msg.lower()
