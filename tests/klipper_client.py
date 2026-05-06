"""
Klipper API client for integration tests.

Connects to Klipper's Unix-domain API socket (JSON-RPC, newline-delimited).
Provides simple synchronous helpers for sending G-code and querying state.

Socket path defaults to /tmp/klippy_uds (Klipper default when started
with  klippy.py ... -a /tmp/klippy_uds).
"""

import json
import logging
import select
import socket
import threading
import time

log = logging.getLogger("klipper_client")

# ---------------------------------------------------------------------------
# SimControl — talks to slave_simulator.py control port (9001 by default)
# ---------------------------------------------------------------------------


class SimControl:
    """
    Thin client for the slave_simulator.py control server.
    Sends JSON commands over TCP to inject/remove filament in simulated slaves.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9001, timeout: float = 5.0):
        self._addr = (host, port)
        self._timeout = timeout

    def _send(self, cmd: dict) -> dict:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(self._timeout)
            s.connect(self._addr)
            s.sendall((json.dumps(cmd) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(256)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf.strip())

    def inject_filament(self, slave_idx: int, slot: int, present: bool = True) -> None:
        reply = self._send(
            {"action": "inject", "slave": slave_idx, "slot": slot, "present": present}
        )
        if not reply.get("ok"):
            raise RuntimeError(f"inject_filament failed: {reply}")

    def inject_filament_delayed(
        self, slave_idx: int, slot: int, present: bool, delay_s: float
    ) -> threading.Timer:
        """
        Schedule an inject_filament call to fire after *delay_s* seconds in
        a background daemon thread.  Returned so callers can cancel.

        Used by integration tests to mimic real hardware: the slave's stepper
        physically pulls filament out during a RETRACT, so the master sensor
        clears mid-operation.  The simulator can't actuate filament, so the
        test side schedules the sensor-clear instead.
        """
        timer = threading.Timer(delay_s, lambda: self.inject_filament(slave_idx, slot, present))
        timer.daemon = True
        timer.start()
        return timer

    def status(self) -> list:
        reply = self._send({"action": "status"})
        if not reply.get("ok"):
            raise RuntimeError(f"status failed: {reply}")
        return reply.get("slaves", [])


# ---------------------------------------------------------------------------
# KlipperClient — Klipper API JSON-RPC client
# ---------------------------------------------------------------------------


class KlipperClient:
    """
    Synchronous Klipper API client.

    Connects to Klipper's Unix-domain JSON-RPC socket.  All calls are
    serialised via a lock so tests can share one instance safely.
    """

    def __init__(self, socket_path: str = "/tmp/klippy_uds"):
        self._path = socket_path
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._next_id = 1
        self._buf = b""
        # Accumulated gcode response lines from async notifications.
        self._gcode_log: list[str] = []

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self._path)
        s.setblocking(False)
        self._sock = s
        self._buf = b""
        self._gcode_log.clear()
        # Subscribe to gcode output so respond_info() reaches us as notifications.
        self.call(
            "gcode/subscribe_output", {"response_template": {"method": "notify_gcode_response"}}
        )

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def wait_ready(self, timeout: float = 60.0) -> None:
        """
        Poll until klippy reports state=="ready" (or "shutdown").
        Reconnects as needed (the socket may not exist yet when klippy starts).
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self._sock is None:
                    self.connect()
                info = self.call("info")
                state = info.get("state", "")
                if state == "ready":
                    return
                if state in ("shutdown", "error"):
                    raise RuntimeError(f"klippy entered state '{state}'")
            except (OSError, ConnectionRefusedError, FileNotFoundError):
                self.close()
            time.sleep(0.5)
        raise TimeoutError(f"klippy did not become ready within {timeout}s")

    # ------------------------------------------------------------------
    # Low-level JSON-RPC
    # ------------------------------------------------------------------

    def _send_raw(self, obj: dict) -> None:
        data = (json.dumps(obj)).encode() + b"\x03"
        if self._sock is None:
            raise OSError("not connected")
        total = 0
        while total < len(data):
            sent = self._sock.send(data[total:])
            if sent == 0:
                raise OSError("socket closed")
            total += sent

    def _recv_line(self, timeout: float = 10.0) -> dict | None:
        """Read one ETX-terminated JSON object from the socket."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if b"\x03" in self._buf:
                idx = self._buf.index(b"\x03")
                line, self._buf = self._buf[:idx], self._buf[idx + 1 :]
                return json.loads(line)
            remaining = deadline - time.monotonic()
            r, _, _ = select.select([self._sock], [], [], min(remaining, 0.1))
            if r:
                chunk = self._sock.recv(4096)
                if not chunk:
                    raise OSError("socket closed by peer")
                self._buf += chunk
        return None

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        """Send a JSON-RPC request and return the result dict."""
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            req = {"id": req_id, "method": method, "params": params or {}}
            self._send_raw(req)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                msg = self._recv_line(timeout=min(remaining, 0.5))
                if msg is None:
                    continue
                # Drain async notifications into the log.
                if "id" not in msg:
                    self._handle_notification(msg)
                    continue
                if msg.get("id") == req_id:
                    if "error" in msg:
                        raise RuntimeError(
                            f"Klipper error: {msg['error'].get('message', msg['error'])}"
                        )
                    return msg.get("result", {})
        raise TimeoutError(f"No response to '{method}' within {timeout}s")

    def _handle_notification(self, msg: dict) -> None:
        if msg.get("method") == "notify_gcode_response":
            params = msg.get("params", {})
            line = params.get("response", "") if isinstance(params, dict) else ""
            if line:
                self._gcode_log.append(line)
                log.debug("gcode: %s", line)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def run_gcode(self, script: str, timeout: float = 60.0) -> None:
        """Execute a G-code script and wait for completion."""
        self.call("gcode/script", {"script": script}, timeout=timeout)

    def query_object(self, obj_name: str) -> dict:
        """Query a single printer object's status."""
        result = self.call("objects/query", {"objects": {obj_name: None}})
        return result.get("status", {}).get(obj_name, {})

    def printer_info(self) -> dict:
        return self.call("info")

    def drain_gcode_log(self) -> list[str]:
        """Return and clear accumulated gcode response messages."""
        with self._lock:
            lines, self._gcode_log = self._gcode_log[:], []
        return lines

    def wait_gcode_contains(self, pattern: str, timeout: float = 30.0) -> list[str]:
        """
        Wait until a gcode response line containing *pattern* appears.
        Returns all accumulated lines up to and including the match.
        Drains the log after returning.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Drain any pending notifications without holding the lock long.
            with self._lock:
                r, _, _ = select.select([self._sock], [], [], 0)
                if r:
                    chunk = self._sock.recv(4096)
                    if chunk:
                        self._buf += chunk
                while b"\x03" in self._buf:
                    idx = self._buf.index(b"\x03")
                    line, self._buf = self._buf[:idx], self._buf[idx + 1 :]
                    obj = json.loads(line)
                    if "id" not in obj:
                        self._handle_notification(obj)

                matched = any(pattern in l for l in self._gcode_log)
                if matched:
                    lines, self._gcode_log = self._gcode_log[:], []
                    return lines
            time.sleep(0.1)
        raise TimeoutError(f"Pattern '{pattern}' not seen in gcode log within {timeout}s")
