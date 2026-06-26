#!/usr/bin/env python3
"""
dolphin_client.py - client for the Colorado Time Systems Dolphin TCP/IP API
(Dolphin software v5+).

ARCHITECTURE
------------
The Dolphin *software* runs the TCP server. This client CONNECTS to it
(host = the PC running Dolphin software, port = the value shown in
Dolphin > Settings (F4) > Streaming > TCP/IP Port), sends an opt-in
handshake, then receives pushed event/heat + race updates. It also exposes
the read-only query commands (getCurrentEventAndHeat, getRaceTimes, ...).

CONFIRM BEFORE TRUSTING PARSING
-------------------------------
Two things are NOT officially documented and must be confirmed on your gear.
Run this file directly in capture mode first:

    python dolphin_client.py --host 192.168.1.50 --port 5000

and watch the raw bytes. Then finalize, at the top of this file:
  * ENCODING            - do4 files are cp1252; confirm on the wire.
  * COMMAND_TERMINATOR  - how the server expects commands to end.
  * how a multi-line "race data" message is delimited (see assemble notes).

SAFETY
------
Commands that mutate Dolphin state (clearEvents, setEventInfo,
setCurrentEventAndHeat, Reset) are BLOCKED unless allow_writes=True, because
a second client running alongside Meet Maestro must not desync the meet.
autoSendUpdates / includeSplitData only affect *your* stream, so they're fine.
"""

from __future__ import annotations

import argparse
import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

log = logging.getLogger("dolphin")

# --------------------------------------------------------------------------- #
# Configuration - confirm these against YOUR install (see module docstring).   #
# --------------------------------------------------------------------------- #
DEFAULT_PORT = 0               # set from Dolphin > Settings (F4) > Streaming
ENCODING = "cp1252"           # do4 files use cp1252; confirm on the wire
COMMAND_TERMINATOR = "\r\n"    # how WE terminate commands we send; confirm

# Commands that change Dolphin's state. Blocked unless allow_writes=True.
DANGEROUS_COMMANDS = {
    "clearevents",
    "seteventinfo",
    "setcurrenteventandheat",
    "reset",
}


# --------------------------------------------------------------------------- #
# Parsed message types                                                         #
# --------------------------------------------------------------------------- #
@dataclass
class CurrentEventHeat:
    version: str
    event_index: int
    event_number: str
    heat_number: int
    race_number: int


@dataclass
class LaneResult:
    lane: int
    final: Optional[float]          # authoritative finish time, seconds
    splits: List[float]             # split times, seconds (may be empty)


@dataclass
class RaceData:
    event_index: Optional[int]
    heat_number: Optional[int]
    lanes: List[LaneResult]
    raw: List[str]                  # the original lines, for debugging


def parse_current_event_and_heat(line: str) -> CurrentEventHeat:
    """Parse the getCurrentEventAndHeat reply.

    Documented shape:
        (version),(eventIndex),(eventNumber),(heatNumber),(raceNumber);
    """
    parts = [p.strip() for p in line.strip().rstrip(";").split(",")]
    if len(parts) < 5:
        raise ValueError(f"unexpected current-event/heat reply: {line!r}")
    return CurrentEventHeat(
        version=parts[0],
        event_index=int(parts[1]),
        event_number=parts[2],
        heat_number=int(parts[3]),
        race_number=int(parts[4]),
    )


def _to_seconds(token: str) -> Optional[float]:
    """Best-effort time parse. '0', '', and non-numerics -> None (no time)."""
    token = token.strip()
    if not token:
        return None
    try:
        val = float(token)
    except ValueError:
        return None
    return val if val > 0 else None


def parse_race_data(lines: List[str]) -> RaceData:
    """Parse a DO4-style race record into lane results.

    STARTING POINT - confirm field layout from a real capture. The do4 file
    format is reverse-engineered (no official spec). Observed convention:
      * a header line carrying event/heat identifiers,
      * one line per lane with up to 3 watch times, semicolon-delimited,
      * a trailing checksum line.
    Times of 0/empty mean "no time for that watch slot".

    Caveat: splits are positional. A missing first split shifts a lane's
    remaining times by one slot - this cannot be auto-detected here.
    """
    rows = [ln for ln in (l.strip() for l in lines) if ln]
    event_index = heat_number = None
    lanes: List[LaneResult] = []

    for i, row in enumerate(rows):
        fields = [f.strip() for f in row.split(";")]
        # Heuristic: a lane line starts with a small integer (the lane number)
        # followed by time-like fields. Refine once you've seen real data.
        if i == 0:
            # header line - try to pull event/heat if numeric
            nums = [f for f in fields if f.isdigit()]
            if len(nums) >= 2:
                event_index, heat_number = int(nums[0]), int(nums[1])
            continue
        if not fields or not fields[0].lstrip("-").isdigit():
            continue  # checksum/footer or non-lane line
        lane = int(fields[0])
        times = [_to_seconds(t) for t in fields[1:]]
        times = [t for t in times if t is not None]
        final = times[0] if times else None
        splits = times[1:] if len(times) > 1 else []
        lanes.append(LaneResult(lane=lane, final=final, splits=splits))

    return RaceData(event_index=event_index, heat_number=heat_number,
                    lanes=lanes, raw=list(lines))


# --------------------------------------------------------------------------- #
# The client                                                                   #
# --------------------------------------------------------------------------- #
class DolphinClient:
    """Threaded TCP client for the Dolphin streaming API.

    Usage:
        client = DolphinClient("192.168.1.50", 5000, on_line=print)
        client.start()           # connects + handshake in a background thread
        ...
        client.stop()

    Or as a context manager:
        with DolphinClient(host, port, on_line=handle) as client:
            ...
    """

    def __init__(
        self,
        host: str,
        port: int,
        on_line: Optional[Callable[[str], None]] = None,
        on_raw: Optional[Callable[[bytes], None]] = None,
        on_connect: Optional[Callable[["DolphinClient"], None]] = None,
        *,
        auto_updates: bool = True,
        include_splits: bool = True,
        allow_writes: bool = False,
        encoding: str = ENCODING,
        connect_timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.on_line = on_line
        self.on_raw = on_raw
        self.on_connect = on_connect
        self.auto_updates = auto_updates
        self.include_splits = include_splits
        self.allow_writes = allow_writes
        self.encoding = encoding
        self.connect_timeout = connect_timeout

        self._sock: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.connected = threading.Event()

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> "DolphinClient":
        self._thread = threading.Thread(target=self._run, name="dolphin-rx",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "DolphinClient":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- connection loop ---------------------------------------------------- #
    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._connect_once()
                backoff = 1.0
                self._reader_loop()        # blocks until disconnect/stop
            except OSError as e:
                log.warning("connection error: %s", e)
            finally:
                self.connected.clear()
                if self._sock is not None:
                    try:
                        self._sock.close()
                    except OSError:
                        pass
                    self._sock = None
            if self._stop.is_set():
                break
            log.info("reconnecting in %.0fs", backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    def _connect_once(self) -> None:
        log.info("connecting to %s:%s", self.host, self.port)
        sock = socket.create_connection((self.host, self.port),
                                        timeout=self.connect_timeout)
        sock.settimeout(None)              # blocking reads after connect
        self._sock = sock
        self.connected.set()
        log.info("connected")

        # (Re)send the handshake every (re)connect - these are per-connection
        # server-side toggles, so they must be re-applied after a reconnect.
        if self.include_splits:
            self.set_include_splits(True)
        if self.auto_updates:
            self.set_auto_updates(True)
        if self.on_connect:
            self.on_connect(self)

    def _reader_loop(self) -> None:
        buf = ""
        assert self._sock is not None
        while not self._stop.is_set():
            chunk = self._sock.recv(4096)
            if not chunk:
                log.info("server closed the connection")
                return
            if self.on_raw:
                self.on_raw(chunk)
            buf += chunk.decode(self.encoding, errors="replace")
            buf = buf.replace("\r\n", "\n").replace("\r", "\n")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if self.on_line:
                    self.on_line(line)

    # -- sending ------------------------------------------------------------ #
    def _send(self, command: str) -> None:
        verb = command.split(",", 1)[0].strip().lower()
        if verb in DANGEROUS_COMMANDS and not self.allow_writes:
            raise PermissionError(
                f"refusing to send state-mutating command {command!r} "
                f"(set allow_writes=True to override - unsafe alongside "
                f"Meet Maestro)"
            )
        sock = self._sock
        if sock is None:
            raise ConnectionError("not connected")
        data = (command + COMMAND_TERMINATOR).encode(self.encoding)
        with self._send_lock:
            sock.sendall(data)
        log.debug("sent: %s", command)

    # -- public commands (read-only / stream toggles) ----------------------- #
    def set_auto_updates(self, on: bool = True) -> None:
        self._send(f"autoSendUpdates,{'ON' if on else 'OFF'}")

    def set_include_splits(self, on: bool = True) -> None:
        self._send(f"includeSplitData,{'ON' if on else 'OFF'}")

    def get_current_event_and_heat(self) -> None:
        self._send("getCurrentEventAndHeat")

    def get_event_info(self) -> None:
        self._send("getEventInfo")

    def get_race_times(self, event_index: int, heat_number: int,
                       show_splits: bool = True) -> None:
        self._send(f"getRaceTimes,{event_index},{heat_number},"
                   f"{'YES' if show_splits else 'NO'}")

    def get_available_races(self, show_splits: bool = True) -> None:
        self._send(f"getAvailableRaces,{'YES' if show_splits else 'NO'}")


# --------------------------------------------------------------------------- #
# CLI: capture mode (default) and self-test                                    #
# --------------------------------------------------------------------------- #
def _capture(host: str, port: int) -> None:
    """Connect and dump everything, so you can confirm framing + encoding."""
    def show_raw(b: bytes) -> None:
        print("RAW", b)                       # repr shows exact bytes

    def show_line(line: str) -> None:
        print("LINE", repr(line))

    client = DolphinClient(host, port, on_line=show_line, on_raw=show_raw)
    print(f"Connecting to {host}:{port}  (Ctrl-C to stop)")
    with client:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nstopping")


def _selftest() -> int:
    """Spin up a fake Dolphin server on localhost and exercise the client.

    Validates the transport, handshake, line framing, and the
    current-event/heat parser without any real hardware.
    """
    received: List[str] = []
    got_handshake: List[str] = []

    def fake_server(sock: socket.socket) -> None:
        conn, _ = sock.accept()
        with conn:
            conn.settimeout(0.3)
            # drain the full handshake (sent as two separate commands)
            deadline = time.time() + 1.0
            while time.time() < deadline:
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    data = b""
                if data:
                    got_handshake.append(data.decode(ENCODING))
                if all(cmd in "".join(got_handshake)
                       for cmd in ("autoSendUpdates,ON", "includeSplitData,ON")):
                    break
            # push a current-event/heat reply and a fake race record
            conn.sendall(("5.0.19,7,52,3,118;" + COMMAND_TERMINATOR)
                         .encode(ENCODING))
            race = COMMAND_TERMINATOR.join([
                "52;3;A",            # header: event 52, heat 3, round A
                "1;58.12;27.40;58.12",
                "2;59.07;28.01;59.07",
                "3;0;0;0",           # empty lane
                "ABC123",            # checksum/footer
                "",
            ])
            conn.sendall(race.encode(ENCODING))
            # keep the connection open until the client closes (no RST noise)
            conn.settimeout(3.0)
            try:
                while conn.recv(4096):
                    pass
            except OSError:
                pass

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(1)
    threading.Thread(target=fake_server, args=(srv,), daemon=True).start()

    client = DolphinClient("127.0.0.1", port, on_line=received.append)
    with client:
        client.connected.wait(timeout=2.0)
        time.sleep(0.8)

    srv.close()

    # --- checks ---
    ok = True
    handshake = "".join(got_handshake)
    if "autoSendUpdates,ON" not in handshake:
        print("FAIL: client did not send autoSendUpdates,ON"); ok = False
    if "includeSplitData,ON" not in handshake:
        print("FAIL: client did not send includeSplitData,ON"); ok = False

    ceh_lines = [l for l in received if l.endswith(";")]
    if not ceh_lines:
        print("FAIL: no current-event/heat line received"); ok = False
    else:
        ceh = parse_current_event_and_heat(ceh_lines[0])
        if not (ceh.event_index == 7 and ceh.heat_number == 3
                and ceh.race_number == 118):
            print(f"FAIL: parsed current-event/heat wrong: {ceh}"); ok = False
        else:
            print(f"ok: current-event/heat -> {ceh}")

    race = parse_race_data([l for l in received if not l.endswith(";")])
    timed = [ln for ln in race.lanes if ln.final is not None]
    if len(timed) != 2:
        print(f"FAIL: expected 2 timed lanes, got {len(timed)}: {race.lanes}")
        ok = False
    else:
        print(f"ok: race data -> {race.lanes}")

    print("\nSELF-TEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="CTS Dolphin TCP/IP client")
    ap.add_argument("--host", default="192.168.1.50")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--selftest", action="store_true",
                    help="run a local fake-server test (no hardware needed)")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()
    if not args.port:
        ap.error("set --port (from Dolphin > Settings > Streaming), "
                 "or use --selftest")
    _capture(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
