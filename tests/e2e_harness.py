"""
Shared test infrastructure for Layer-3 end-to-end integration tests.

Handles the MicroPython shim bootstrap, exposes PipedUART / SlaveNode /
BusMaster / BusMasterMulti for reuse across test files.

Import this module BEFORE any slave-side code.  It is idempotent — safe
to import from multiple test files in the same process.
"""

import asyncio
import importlib
import importlib.util
import os
import select
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SHIMS_DIR = os.path.join(REPO_ROOT, "tests", "micropython_shims")
SLAVE_DIR = os.path.join(REPO_ROOT, "slave")

for _p in (SLAVE_DIR, SHIMS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Patch stdlib 'time' with MicroPython-style helpers (in-place)
# ---------------------------------------------------------------------------
import time as _real_time  # noqa: E402

if not hasattr(_real_time, "ticks_ms"):
    _real_time.ticks_ms = lambda: int(_real_time.monotonic() * 1000)
    _real_time.ticks_diff = lambda newer, older: newer - older
    _real_time.ticks_add = lambda t, d: t + d
if not hasattr(_real_time, "sleep_us"):
    _real_time.sleep_us = lambda us: None
if not hasattr(_real_time, "sleep_ms"):
    _real_time.sleep_ms = lambda ms: None

# ---------------------------------------------------------------------------
# Inject MicroPython shim modules
# ---------------------------------------------------------------------------


def _load_shim(name: str):
    path = os.path.join(SHIMS_DIR, name.replace(".", os.sep) + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


for _shim_name in ("machine", "uasyncio", "rp2", "urandom"):
    if _shim_name not in sys.modules:
        _load_shim(_shim_name)

# ---------------------------------------------------------------------------
# Slave imports (after shim bootstrap)
# ---------------------------------------------------------------------------
from bus.protocol import (  # noqa: E402
    Addr,
    Cmd,
    FrameReader,
    LedMode,
    SlotState,
    Status,
    build_frame,
)
from state_machine import SlaveController  # noqa: E402

# ---------------------------------------------------------------------------
# PipedUART — os.pipe-backed drop-in for machine.UART
# ---------------------------------------------------------------------------


class PipedUART:
    """
    Drop-in replacement for machine.UART backed by os.pipe file descriptors.

    read_fd:  bytes appear here when the master sends a command (slave reads)
    write_fd: slave writes response bytes here (master reads from the other end)
    """

    def __init__(self, read_fd: int, write_fd: int):
        self._rfd = read_fd
        self._wfd = write_fd
        self._pending = b""
        import fcntl

        fl = fcntl.fcntl(self._rfd, fcntl.F_GETFL)
        fcntl.fcntl(self._rfd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    def any(self) -> int:
        r, _, _ = select.select([self._rfd], [], [], 0)
        if r:
            self._pending += os.read(self._rfd, 256)
        return len(self._pending)

    def read(self, nbytes=None) -> bytes:
        if self._pending:
            if nbytes is None or nbytes >= len(self._pending):
                data, self._pending = self._pending, b""
                return data
            data, self._pending = self._pending[:nbytes], self._pending[nbytes:]
            return data
        try:
            return os.read(self._rfd, nbytes or 256)
        except BlockingIOError:
            return b""

    def write(self, data) -> int:
        os.write(self._wfd, data)
        return len(data)

    def readinto(self, buf, nbytes=None) -> int:
        n = nbytes if nbytes is not None else len(buf)
        data = self.read(n)
        buf[: len(data)] = data
        return len(data)


# ---------------------------------------------------------------------------
# SlaveNode — SlaveController + piped UART running in a daemon thread
# ---------------------------------------------------------------------------


class SlaveNode:
    """
    Wraps a SlaveController, replacing its RS485 UART with os.pipes and
    running the asyncio event loop in a daemon thread.
    """

    def __init__(self, assigned_addr=None):
        # cmd pipe: master writes → slave reads
        self._cmd_r, self._cmd_w = os.pipe()
        # resp pipe: slave writes → master reads
        self._resp_r, self._resp_w = os.pipe()
        import fcntl

        fl = fcntl.fcntl(self._resp_r, fcntl.F_GETFL)
        fcntl.fcntl(self._resp_r, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        self._ctrl = SlaveController()
        # _load_address() reads addr.cfg which may be stale from a prior test
        # run or from test_e2e_discover_assign saving an address.  Always
        # honour the caller's explicit intent; default to unassigned (0xFF).
        if assigned_addr is not None:
            self._ctrl._addr = assigned_addr
        else:
            self._ctrl._addr = 0xFF  # DEVICE_ADDR_UNASSIGNED
        self._ctrl._rs485._uart = PipedUART(self._cmd_r, self._resp_w)

        self._thread = None
        self._loop = None
        self._task = None
        self._ready = threading.Event()

    @property
    def ctrl(self) -> SlaveController:
        return self._ctrl

    @property
    def addr(self) -> int:
        return self._ctrl._addr

    def start(self):
        """Start the slave asyncio loop in a daemon thread."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._task = self._loop.create_task(self._ctrl.run())
        self._ready.set()
        try:
            self._loop.run_until_complete(self._task)
        except (asyncio.CancelledError, RuntimeError):
            pass
        finally:
            pending = asyncio.all_tasks(self._loop)
            for t in pending:
                t.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    def stop(self):
        if self._loop and self._task:
            self._loop.call_soon_threadsafe(self._task.cancel)
        if self._thread:
            self._thread.join(timeout=1.0)

    def inject_filament(self, slot: int, present: bool = True):
        """
        Simulate filament insertion/removal.

        Sets the underlying pin value only; the slave's sensor_loop detects
        the change after the debounce window (SENSOR_DEBOUNCE_MS = 5 ms).
        Caller must sleep long enough for the change to propagate.
        Active LOW: filament present → pin = 0; absent → pin = 1.
        """
        self._ctrl._sensors.sensors[slot]._pin._value = 0 if present else 1


# ---------------------------------------------------------------------------
# BusMaster — single-slave frame send/receive
# ---------------------------------------------------------------------------


class BusMaster:
    """Sends RS485 frames to a single SlaveNode and reads responses."""

    RESPONSE_TIMEOUT = 1.0  # seconds

    def __init__(self, slave: SlaveNode):
        self._slave = slave
        self._seq = 0
        self._reader = FrameReader()

    def _next_seq(self) -> int:
        s = self._seq
        self._seq = (self._seq + 1) & 0xFF
        return s

    def send(self, addr, cmd, data=b""):
        """Fire-and-forget (no response expected)."""
        frame = build_frame(addr, cmd, self._next_seq(), data)
        os.write(self._slave._cmd_w, frame)

    def query(self, addr, cmd, data=b"", timeout=None) -> bytes | None:
        """Send frame, wait for matching response; returns payload or None."""
        if timeout is None:
            timeout = self.RESPONSE_TIMEOUT
        seq = self._next_seq()
        frame = build_frame(addr, cmd, seq, data)
        os.write(self._slave._cmd_w, frame)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            r, _, _ = select.select([self._slave._resp_r], [], [], min(remaining, 0.05))
            if r:
                self._reader.feed(os.read(self._slave._resp_r, 256))
            parsed = self._reader.try_parse()
            if parsed is not None and parsed.is_response and parsed.seq == seq:
                return parsed.data
        return None


# ---------------------------------------------------------------------------
# BusMasterMulti — multi-slave shared-bus simulation
# ---------------------------------------------------------------------------


class BusMasterMulti:
    """
    Simulates an RS485 bus with multiple slave nodes.

    Each slave has its own cmd/resp pipe pair.  Broadcast sends write to every
    slave's cmd pipe.  collect_responses() polls all resp pipes.
    """

    def __init__(self, slaves: list):
        self._slaves = slaves
        self._seq = 0

    def _next_seq(self) -> int:
        s = self._seq
        self._seq = (self._seq + 1) & 0xFF
        return s

    def broadcast_send(self, cmd, data=b""):
        """Send a broadcast frame to all slaves."""
        frame = build_frame(Addr.BROADCAST, cmd, self._next_seq(), data)
        for slave in self._slaves:
            os.write(slave._cmd_w, frame)

    def send_to_all(self, addr, cmd, data=b""):
        """Send a frame addressed to 'addr' on every slave's cmd pipe."""
        frame = build_frame(addr, cmd, self._next_seq(), data)
        for slave in self._slaves:
            os.write(slave._cmd_w, frame)

    def query_slave(self, slave: SlaveNode, addr, cmd, data=b"", timeout=1.0) -> bytes | None:
        """Send to one slave's pipe and wait for the response."""
        seq = self._next_seq()
        frame = build_frame(addr, cmd, seq, data)
        os.write(slave._cmd_w, frame)
        reader = FrameReader()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            r, _, _ = select.select([slave._resp_r], [], [], min(remaining, 0.05))
            if r:
                reader.feed(os.read(slave._resp_r, 256))
            parsed = reader.try_parse()
            if parsed is not None and parsed.is_response and parsed.seq == seq:
                return parsed.data
        return None

    def collect_responses(self, expected_cmd, timeout=0.5) -> list:
        """
        Poll all resp pipes for responses to expected_cmd within timeout.
        Returns list of (SlaveNode, parsed_frame) tuples.
        """
        fd_to_slave = {s._resp_r: s for s in self._slaves}
        readers = {s._resp_r: FrameReader() for s in self._slaves}
        responses = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            fds = list(fd_to_slave.keys())
            r, _, _ = select.select(fds, [], [], min(remaining, 0.05))
            for fd in r:
                chunk = os.read(fd, 256)
                readers[fd].feed(chunk)
            for fd, reader in readers.items():
                parsed = reader.try_parse()
                if parsed is not None and parsed.is_response:
                    responses.append((fd_to_slave[fd], parsed))
        return responses


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bus(assigned_addr=0x01):
    """Create a running SlaveNode and a connected BusMaster."""
    slave = SlaveNode(assigned_addr=assigned_addr)
    slave.start()
    return BusMaster(slave), slave


def _load_cfg(name: str):
    """Load a named constant from slave/config.py."""
    path = os.path.join(REPO_ROOT, "slave", "config.py")
    spec = importlib.util.spec_from_file_location("_slave_cfg_loader", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return getattr(m, name)
