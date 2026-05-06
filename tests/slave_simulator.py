"""
Slave Simulator — TCP gateway between the Klipper Linux MCU and real slave
state machines (from tests/e2e_harness.py).

The Linux MCU (klipper.elf) connects here over TCP on port 9000.  The
simulator routes PMU binary frames to per-slave SlaveNode instances using
the same os.pipe infrastructure that the e2e tests use.

Usage:
    python tests/slave_simulator.py [--slaves N] [--port PORT] [--host HOST]

Environment-variable equivalents (lower priority than CLI args):
    SLAVE_COUNT   number of slave nodes   (default 2)
    SLAVE_SIM_PORT  TCP port to listen on (default 9000)
    SLAVE_SIM_HOST  bind address          (default 127.0.0.1)

Each slave starts as UNASSIGNED (addr=0xFF).  The Klipper pico_mmu plugin
runs MMU_HOME to discover and assign addresses — exactly as it would on
real hardware.

Filament injection:
    The simulator accepts a secondary control connection on (port+1, default
    9001) to allow test code to inject/remove filament during a test.
    Wire format: one JSON line per command:
        {"action":"inject","slave":0,"slot":1,"present":true}
    Response: one JSON line {"ok":true} or {"ok":false,"error":"..."}
"""

import argparse
import json
import logging
import os
import select
import socket
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Ensure project paths are on sys.path before importing harness helpers.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# e2e_harness bootstraps all MicroPython shims internally — just import it.
from e2e_harness import (
    SlaveNode,  # noqa: E402
    build_frame,  # noqa: E402
)

# Re-use the protocol helpers from the slave bus layer.
SLAVE_DIR = os.path.join(_ROOT, "slave")
if SLAVE_DIR not in sys.path:
    sys.path.insert(0, SLAVE_DIR)

from bus.protocol import Addr, FrameReader  # noqa: E402

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [sim] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("slave_sim")

# ---------------------------------------------------------------------------
# SlaveRouter — maps received frames to SlaveNode pipes, reads back responses
# ---------------------------------------------------------------------------


class SlaveRouter:
    """
    Owns all SlaveNode instances and handles per-frame routing.

    Frame routing rules:
    - BROADCAST (addr=0x00): write to every slave's cmd pipe.
    - UNASSIGNED (addr=0xFF): write to every unassigned slave's cmd pipe.
    - 0x01-0xFE: write to the slave whose assigned addr matches.
    """

    # Deterministic UID base: deadbeef000000<index>
    # Slave 0 → deadbeef00000000, Slave 1 → deadbeef00000001, …
    _UID_PREFIX = bytes.fromhex("deadbeef000000")  # 7 bytes; last byte = slave index

    def __init__(self, n_slaves: int):
        self._slaves: list[SlaveNode] = []
        for i in range(n_slaves):
            node = SlaveNode()
            # Assign a deterministic, unique 8-byte UID per slave index.
            node.ctrl._unique_id = self._UID_PREFIX + bytes([i])
            self._slaves.append(node)
        for s in self._slaves:
            s.start()
        log.info("Started %d slave node(s)", n_slaves)

    def stop(self):
        for s in self._slaves:
            s.stop()

    def route(self, raw_frame: bytes, frame) -> None:
        """Write raw_frame bytes to the appropriate slave cmd pipe(s)."""
        target = frame.addr

        if target == int(Addr.BROADCAST):
            pipes = [s._cmd_w for s in self._slaves]
        elif target == int(Addr.UNASSIGNED):
            pipes = [s._cmd_w for s in self._slaves if s.addr == 0xFF]
        else:
            pipes = [s._cmd_w for s in self._slaves if s.addr == target]

        if not pipes:
            log.debug("No slave for addr=0x%02X — dropping frame", target)
            return

        for pipe in pipes:
            os.write(pipe, raw_frame)

    def response_fds(self) -> list[int]:
        return [s._resp_r for s in self._slaves]

    def inject_filament(self, slave_idx: int, slot: int, present: bool) -> None:
        self._slaves[slave_idx].inject_filament(slot, present)

    @property
    def slave_count(self) -> int:
        return len(self._slaves)


# ---------------------------------------------------------------------------
# TCPBridge — accepts connections and pumps bytes between TCP and pipes
# ---------------------------------------------------------------------------


class TCPBridge:
    """
    Accepts a single MCU TCP connection and mediates:
    - incoming bytes → parse frames → SlaveRouter.route()
    - response bytes from slave pipes → write back to TCP socket
    """

    def __init__(self, router: SlaveRouter, host: str, port: int):
        self._router = router
        self._host = host
        self._port = port
        self._server_sock: socket.socket | None = None
        self._client_sock: socket.socket | None = None
        self._reader = FrameReader()
        self._lock = threading.Lock()
        self._running = False

    # ------------------------------------------------------------------
    def start(self) -> None:
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((self._host, self._port))
        self._server_sock.listen(1)
        self._server_sock.setblocking(False)
        self._running = True
        log.info("Listening for MCU connection on %s:%d", self._host, self._port)

    def stop(self) -> None:
        self._running = False
        if self._client_sock:
            try:
                self._client_sock.close()
            except OSError:
                pass
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    def run_forever(self) -> None:
        """Main event loop — accept → route → respond."""
        while self._running:
            # Wait for a client connection.
            r, _, _ = select.select([self._server_sock], [], [], 0.5)
            if not r:
                continue

            try:
                client, addr = self._server_sock.accept()
            except OSError:
                break

            client.setblocking(False)
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._client_sock = client
            self._reader = FrameReader()
            log.info("MCU connected from %s:%d", addr[0], addr[1])

            self._serve_client(client)
            log.info("MCU disconnected")
            self._client_sock = None

    def _serve_client(self, client: socket.socket) -> None:
        resp_fds = self._router.response_fds()
        while self._running:
            all_fds = [client.fileno()] + resp_fds
            try:
                r, _, e = select.select(all_fds, [], all_fds, 0.1)
            except (ValueError, OSError):
                break

            if e:
                break

            for fd in r:
                if fd == client.fileno():
                    # Bytes from the MCU → parse frames → route to slaves.
                    try:
                        chunk = client.recv(512)
                    except OSError:
                        return
                    if not chunk:
                        return  # MCU closed connection

                    self._reader.feed(chunk)
                    while True:
                        frame = self._reader.try_parse()
                        if frame is None:
                            break
                        raw = self._rebuild_raw(frame)
                        log.debug(
                            "MCU→slave: addr=0x%02X cmd=0x%02X seq=%d len=%d",
                            frame.addr,
                            frame.cmd,
                            frame.seq,
                            len(frame.data),
                        )
                        self._router.route(raw, frame)

                else:
                    # Bytes from a slave response pipe → forward to MCU.
                    try:
                        data = os.read(fd, 256)
                    except OSError:
                        continue
                    if data:
                        log.debug("slave→MCU: %d bytes", len(data))
                        try:
                            client.sendall(data)
                        except OSError:
                            return

    @staticmethod
    def _rebuild_raw(frame) -> bytes:
        """Reconstruct the raw frame bytes from a parsed Frame object."""
        cmd = frame.cmd | (0x80 if frame.is_response else 0x00)
        return build_frame(frame.addr, cmd, frame.seq, frame.data)


# ---------------------------------------------------------------------------
# ControlServer — accepts test-side control connections (filament injection)
# ---------------------------------------------------------------------------


class ControlServer:
    """
    Simple line-oriented JSON control server on (main_port + 1).

    Accepts commands:
        {"action":"inject","slave":0,"slot":1,"present":true}
        {"action":"status"}

    Responds with one JSON line per command.
    """

    def __init__(self, router: SlaveRouter, host: str, port: int):
        self._router = router
        self._host = host
        self._port = port
        self._server_sock: socket.socket | None = None
        self._running = False

    def start(self) -> None:
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((self._host, self._port))
        self._server_sock.listen(8)
        self._server_sock.setblocking(False)
        self._running = True
        log.info("Control server on %s:%d", self._host, self._port)
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def stop(self) -> None:
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass

    def _run(self) -> None:
        while self._running:
            r, _, _ = select.select([self._server_sock], [], [], 0.5)
            if not r:
                continue
            try:
                client, _ = self._server_sock.accept()
            except OSError:
                break
            ct = threading.Thread(target=self._handle, args=(client,), daemon=True)
            ct.start()

    def _handle(self, conn: socket.socket) -> None:
        buf = b""
        conn.settimeout(10.0)
        try:
            while True:
                chunk = conn.recv(256)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    reply = self._dispatch(line.strip())
                    conn.sendall((json.dumps(reply) + "\n").encode())
        except (OSError, TimeoutError):
            pass
        finally:
            conn.close()

    def _dispatch(self, line: bytes) -> dict:
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": str(exc)}

        action = cmd.get("action")

        if action == "inject":
            slave_idx = int(cmd.get("slave", 0))
            slot = int(cmd.get("slot", 0))
            present = bool(cmd.get("present", True))
            if slave_idx >= self._router.slave_count:
                return {"ok": False, "error": f"slave index {slave_idx} out of range"}
            self._router.inject_filament(slave_idx, slot, present)
            # Give sensor debounce loop time to settle.
            time.sleep(0.01)
            return {"ok": True}

        if action == "status":
            slaves_info = []
            for i, s in enumerate(self._router._slaves):
                slaves_info.append({"index": i, "addr": s.addr})
            return {"ok": True, "slaves": slaves_info}

        return {"ok": False, "error": f"unknown action '{action}'"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pico-MMU slave simulator")
    p.add_argument(
        "--slaves",
        type=int,
        default=int(os.environ.get("SLAVE_COUNT", "2")),
        help="Number of simulated slave nodes (default: 2)",
    )
    p.add_argument(
        "--host",
        default=os.environ.get("SLAVE_SIM_HOST", "127.0.0.1"),
        help="Bind address (default: 127.0.0.1)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SLAVE_SIM_PORT", "9000")),
        help="TCP port for MCU connection (default: 9000)",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    router = SlaveRouter(args.slaves)
    bridge = TCPBridge(router, args.host, args.port)
    control = ControlServer(router, args.host, args.port + 1)

    bridge.start()
    control.start()

    try:
        bridge.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
        control.stop()
        router.stop()


if __name__ == "__main__":
    main()
