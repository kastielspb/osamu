"""
Unit tests for Infinite Spool (slot-chain auto-switch) logic in pico_mmu.py.

Exercises the Klipper-side orchestration code in isolation — no Klipper runtime
or RS485 hardware is required.  PicoMmu instances are created via __new__() and
populated with hand-crafted attributes.

Scenarios covered:
  IS-1  _build_slot_groups — explicit slot_groups config
  IS-2  _build_slot_groups — auto-group by matching filament color
  IS-3  _build_slot_groups — explicit entry overrides / suppresses auto-color group
  IS-4  _get_group — ungrouped tool returns None
  IS-5  _find_backup_tool — backup LOADED slot found
  IS-6  _find_backup_tool — all group members empty, returns None
  IS-7  _handle_runout — backup available → _do_switch_to_backup() called
  IS-8  _handle_runout — no backup → PAUSE + STATE_ERROR, no load attempt
"""

import importlib.util
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
SlotState = _mod.SlotState
PicoMmuError = _mod.PicoMmuError
STATE_PRINTING = _mod.STATE_PRINTING
STATE_ERROR = _mod.STATE_ERROR
STATE_TIP_FORMING = _mod.STATE_TIP_FORMING


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeSlave:
    """Minimal slave stand-in for group-resolution tests."""

    def __init__(self, slots, slot_states=None, online=True, addr=1):
        self.slots = slots
        self.slot_colors = [(0, 255, 0)] * len(slots)
        self.slot_states = slot_states if slot_states is not None else [SlotState.EMPTY] * 4
        self.online = online
        self.addr = addr


def make_mmu(
    *,
    slot_groups=None,
    tool_colors=None,
    slaves=None,
    state=STATE_PRINTING,
    current_tool=0,
):
    """
    Build a PicoMmu instance without calling __init__ (no Klipper runtime).
    """
    mmu = PicoMmu.__new__(PicoMmu)
    mmu._slot_groups = slot_groups or {}
    mmu._tool_colors = tool_colors or {}
    mmu._effective_groups = {}
    mmu._runout_scheduled = False
    mmu.state = state
    mmu.current_tool = current_tool
    mmu.slaves = slaves or []
    mmu.gcode = MagicMock()
    mmu.reactor = MagicMock()
    return mmu


# ---------------------------------------------------------------------------
# IS-1: explicit slot_groups config
# ---------------------------------------------------------------------------


def test_build_groups_explicit():
    """slot_groups: 1,0,1,0 → T0 and T2 in group 1; T1 and T3 ungrouped."""
    mmu = make_mmu(
        slot_groups={0: 1, 1: 0, 2: 1, 3: 0},
        tool_colors={0: (255, 0, 0), 1: (0, 255, 0), 2: (255, 0, 0), 3: (0, 255, 0)},
    )
    mmu._build_slot_groups()

    assert mmu._get_group(0) == 1
    assert mmu._get_group(2) == 1
    assert mmu._get_group(0) == mmu._get_group(2)
    assert mmu._get_group(1) is None
    assert mmu._get_group(3) is None


# ---------------------------------------------------------------------------
# IS-2: auto-group by matching filament color
# ---------------------------------------------------------------------------


def test_build_groups_auto_color():
    """
    No explicit groups.  Tools sharing the same color are auto-grouped when
    two or more tools share that color.
    """
    mmu = make_mmu(
        tool_colors={
            0: (255, 0, 0),  # red
            1: (0, 255, 0),  # green
            2: (255, 0, 0),  # red — same as T0
            3: (0, 255, 0),  # green — same as T1
        },
    )
    mmu._build_slot_groups()

    g0 = mmu._get_group(0)
    g2 = mmu._get_group(2)
    g1 = mmu._get_group(1)
    g3 = mmu._get_group(3)

    assert g0 is not None, "T0 must be auto-grouped (shares red with T2)"
    assert g0 == g2, "T0 and T2 must share the same auto-group"
    assert g1 is not None, "T1 must be auto-grouped (shares green with T3)"
    assert g1 == g3, "T1 and T3 must share the same auto-group"
    assert g0 != g1, "Red group and green group must be different"


def test_build_groups_unique_color_not_grouped():
    """A tool with a unique color must NOT be auto-grouped (it has no partner)."""
    mmu = make_mmu(
        tool_colors={0: (255, 0, 0), 1: (0, 255, 0), 2: (0, 0, 255)},
    )
    mmu._build_slot_groups()

    assert mmu._get_group(0) is None
    assert mmu._get_group(1) is None
    assert mmu._get_group(2) is None


# ---------------------------------------------------------------------------
# IS-3: explicit entry suppresses or overrides auto-color
# ---------------------------------------------------------------------------


def test_build_groups_explicit_overrides_color():
    """
    T0 has explicit group 0 (ungrouped) and T2 has explicit group 0.
    Even though T0 and T2 share a color they must NOT be auto-grouped.
    """
    mmu = make_mmu(
        slot_groups={0: 0, 2: 0},
        tool_colors={0: (255, 0, 0), 1: (0, 255, 0), 2: (255, 0, 0)},
    )
    mmu._build_slot_groups()

    assert mmu._get_group(0) is None, "T0 explicit 0 must remain ungrouped"
    assert mmu._get_group(2) is None, "T2 explicit 0 must remain ungrouped"


def test_build_groups_explicit_nonzero_overrides_auto():
    """
    T0 and T1 share a color (would be auto-grouped).
    T0 is explicitly assigned to group 5 instead.
    T0 must be in group 5; T1 is alone in the auto-group (effectively ungrouped
    because no backup exists, but still has its own group ID).
    """
    mmu = make_mmu(
        slot_groups={0: 5},
        tool_colors={0: (255, 0, 0), 1: (255, 0, 0), 2: (0, 255, 0)},
    )
    mmu._build_slot_groups()

    assert mmu._get_group(0) == 5, "T0 must be in explicit group 5"
    # T1 was ungrouped by config (explicit.get(1,0)==0) → goes into auto,
    # but is alone since T0 was pulled out → auto_group of size 1 is NOT created
    assert mmu._get_group(1) is None, (
        "T1 is the only ungrouped tool with red; must not form a singleton auto-group"
    )


# ---------------------------------------------------------------------------
# IS-4: _get_group on an ungrouped tool
# ---------------------------------------------------------------------------


def test_get_group_ungrouped():
    """A tool not present in _effective_groups returns None."""
    mmu = make_mmu()
    mmu._effective_groups = {0: 1}

    assert mmu._get_group(0) == 1
    assert mmu._get_group(1) is None
    assert mmu._get_group(99) is None


# ---------------------------------------------------------------------------
# IS-5: _find_backup_tool — backup found
# ---------------------------------------------------------------------------


def test_find_backup_loaded():
    """T2 is LOADED and in the same group as T0 → _find_backup_tool returns T2."""
    slave = FakeSlave(
        slots=[0, 1, 2, 3],
        slot_states=[SlotState.EMPTY, SlotState.EMPTY, SlotState.LOADED, SlotState.EMPTY],
    )
    mmu = make_mmu(slaves=[slave], current_tool=0)
    mmu._effective_groups = {0: 1, 2: 1}

    backup = mmu._find_backup_tool(0)
    assert backup == 2


def test_find_backup_skips_non_loaded():
    """T2 is FEEDING (not LOADED) — must not be selected as backup."""
    slave = FakeSlave(
        slots=[0, 1, 2, 3],
        slot_states=[SlotState.EMPTY, SlotState.EMPTY, SlotState.FEEDING, SlotState.LOADED],
    )
    mmu = make_mmu(slaves=[slave], current_tool=0)
    mmu._effective_groups = {0: 1, 2: 1, 3: 1}

    backup = mmu._find_backup_tool(0)
    assert backup == 3, "Must skip FEEDING slot and pick the LOADED one"


# ---------------------------------------------------------------------------
# IS-6: _find_backup_tool — all group members empty / unavailable
# ---------------------------------------------------------------------------


def test_find_backup_all_empty():
    """All group partners are EMPTY → returns None."""
    slave = FakeSlave(
        slots=[0, 1, 2, 3],
        slot_states=[SlotState.EMPTY, SlotState.EMPTY, SlotState.EMPTY, SlotState.EMPTY],
    )
    mmu = make_mmu(slaves=[slave], current_tool=0)
    mmu._effective_groups = {0: 1, 2: 1}

    assert mmu._find_backup_tool(0) is None


def test_find_backup_ungrouped_returns_none():
    """Tool not in any group → _find_backup_tool returns None immediately."""
    slave = FakeSlave(
        slots=[0, 1, 2, 3],
        slot_states=[SlotState.EMPTY, SlotState.LOADED, SlotState.LOADED, SlotState.LOADED],
    )
    mmu = make_mmu(slaves=[slave], current_tool=0)
    mmu._effective_groups = {}  # T0 ungrouped

    assert mmu._find_backup_tool(0) is None


# ---------------------------------------------------------------------------
# IS-7: _handle_runout — backup available → _do_switch_to_backup called
# ---------------------------------------------------------------------------


def test_handle_runout_triggers_switch():
    """When a backup slot is available, _do_switch_to_backup() must be called."""
    slave = FakeSlave(
        slots=[0, 1, 2, 3],
        slot_states=[SlotState.EMPTY, SlotState.EMPTY, SlotState.LOADED, SlotState.EMPTY],
    )
    mmu = make_mmu(slaves=[slave], state=STATE_PRINTING, current_tool=0)
    mmu._effective_groups = {0: 1, 2: 1}

    with patch.object(mmu, "_do_switch_to_backup") as mock_switch:
        mmu._handle_runout(eventtime=0.0)
        mock_switch.assert_called_once_with(2)

    # _runout_scheduled must be cleared so future runouts can fire
    assert mmu._runout_scheduled is False


def test_handle_runout_noop_when_not_printing():
    """_handle_runout must do nothing if the MMU is not STATE_PRINTING."""
    mmu = make_mmu(state=STATE_ERROR, current_tool=0)
    mmu._effective_groups = {0: 1, 2: 1}

    with patch.object(mmu, "_do_switch_to_backup") as mock_switch:
        mmu._handle_runout(eventtime=0.0)
        mock_switch.assert_not_called()
    mmu.gcode.run_script_from_command.assert_not_called()


# ---------------------------------------------------------------------------
# IS-8: _handle_runout — no backup → PAUSE + STATE_ERROR, no load
# ---------------------------------------------------------------------------


def test_handle_runout_no_backup_pauses():
    """When no backup is available, print must be paused and state set to ERROR."""
    slave = FakeSlave(
        slots=[0, 1, 2, 3],
        slot_states=[SlotState.EMPTY, SlotState.EMPTY, SlotState.EMPTY, SlotState.EMPTY],
    )
    mmu = make_mmu(slaves=[slave], state=STATE_PRINTING, current_tool=0)
    mmu._effective_groups = {0: 1, 2: 1}

    with patch.object(mmu, "_do_switch_to_backup") as mock_switch:
        mmu._handle_runout(eventtime=0.0)
        mock_switch.assert_not_called()

    assert mmu.state == STATE_ERROR
    mmu.gcode.run_script_from_command.assert_called_with("PAUSE")
    mmu.gcode.respond_info.assert_called_once()
    msg = mmu.gcode.respond_info.call_args[0][0]
    assert "no loaded backup" in msg.lower() or "pausing" in msg.lower()
