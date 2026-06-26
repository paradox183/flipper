#!/usr/bin/env python3
"""
dolphin_usb_decoder.py - decode the CTS Dolphin USB base-unit stream for
REAL-TIME, per-lane finish detection (all 10 lanes), well before the Reset
that the TCP/IP socket waits for.

WHAT THIS GIVES YOU (validated by reverse engineering)
------------------------------------------------------
The base (FTDI FT232R, USB 0403:6001) streams a fixed 352-byte status frame
~3.4x/sec on bulk IN endpoint 0x81. Each frame carries a per-watch state byte
for 10 lanes x 3 timers (A/B/C):

    state_offset(lane, watch) = 19 + (lane-1)*33 + watch*11      # watch: A=0,B=1,C=2

    0x82 = idle      0x83 = armed/running (after the gun)      0x81 = time recorded

Watching those bytes flip to 0x81 detects each watch stop within ~one frame
(~290 ms) of the touch. The lane map was confirmed on lanes 1-6 (18 watch
events) and extrapolates cleanly to lanes 7-10 (lane 10's last record ends at
byte 348, inside the 352-byte frame).

WHAT THIS DOES NOT GIVE YOU
---------------------------
The precise 0.01s final time is NOT on the wire in any usable encoding - the
in-frame time field is only a coarse running value (+-0.4-0.5 s vs the official
time). The exact final is computed by the Dolphin software and should be taken
from the TCP/IP socket (dolphin_client.py) or the result file at Reset. The
intended pairing: use THIS for instant "lane X touched" events (freeze that
lane's start-tap running clock), then overwrite with the authoritative time
when Reset fires.

FEEDING IT LIVE
---------------
The Dolphin software holds the FTDI device open via the D2XX driver, so a second
opener (pyusb) would conflict. Read passively instead:
  * offline / now:  frames_from_pcapng("capture.pcapng")  (uses tshark)
  * live:           frames_from_pyshark()  (USBPcap live; pip install pyshark)
Either way the firmware-string frame anchor below may need updating if CTS ships
new base firmware (current: "1.51").
"""

from __future__ import annotations

import argparse
import socket
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, Optional, Tuple

# --------------------------------------------------------------------------- #
# Protocol constants (confirm if firmware changes)                            #
# --------------------------------------------------------------------------- #
FTDI_VID, FTDI_PID = 0x0403, 0x6001
FRAME_ANCHOR = bytes.fromhex("312e3531")   # firmware version "1.51"
FRAME_LEN = 352
NUM_LANES = 10
WATCHES = ("A", "B", "C")

S_IDLE, S_RUNNING, S_RECORDED = 0x82, 0x83, 0x81
STATE_NAME = {S_IDLE: "idle", S_RUNNING: "running", S_RECORDED: "recorded"}


def state_offset(lane: int, watch: int) -> int:
    """Byte offset of the state code for (lane 1-10, watch 0=A/1=B/2=C)."""
    return 19 + (lane - 1) * 33 + watch * 11


# --------------------------------------------------------------------------- #
# Frame reassembly from a logical serial byte stream                          #
# --------------------------------------------------------------------------- #
def reassemble_frames(chunks: Iterator[Tuple[float, bytes]]
                      ) -> Iterator[Tuple[float, bytes]]:
    """Turn (timestamp, payload-bytes) chunks into (timestamp, 352-byte frame).

    Anchors on the firmware string, then locks to a fixed FRAME_LEN stride so a
    one-off corrupt/odd byte doesn't desync us. The timestamp reported for a
    frame is that of the chunk in which the frame's anchor appeared.
    """
    buf = bytearray()
    stamps = []          # (absolute_offset_consumed, timestamp) for buffered bytes
    consumed = 0
    locked = False
    while True:
        try:
            ts, data = next(chunks)
        except StopIteration:
            return
        stamps.append((consumed + len(buf), ts))
        buf += data

        while True:
            if not locked:
                i = buf.find(FRAME_ANCHOR)
                if i < 0:
                    break
                if len(buf) - i < FRAME_LEN:
                    # keep from anchor onward, wait for more
                    del buf[:i]
                    consumed += i
                    break
                frame = bytes(buf[i:i + FRAME_LEN])
                ts_for = _stamp_for(consumed + i, stamps)
                del buf[:i + FRAME_LEN]
                consumed += i + FRAME_LEN
                locked = True
                yield ts_for, frame
            else:
                if len(buf) < FRAME_LEN:
                    break
                # resync if the anchor isn't where we expect (firmware/desync)
                if buf[:len(FRAME_ANCHOR)] != FRAME_ANCHOR:
                    locked = False
                    continue
                frame = bytes(buf[:FRAME_LEN])
                ts_for = _stamp_for(consumed, stamps)
                del buf[:FRAME_LEN]
                consumed += FRAME_LEN
                yield ts_for, frame
        # drop stamps we've passed
        stamps[:] = [(o, t) for (o, t) in stamps if o >= consumed - len(buf)]


def _stamp_for(abs_off: int, stamps) -> float:
    ts = stamps[0][1] if stamps else 0.0
    for o, t in stamps:
        if o <= abs_off:
            ts = t
        else:
            break
    return ts


# --------------------------------------------------------------------------- #
# Per-lane / per-watch state tracking + events                                #
# --------------------------------------------------------------------------- #
@dataclass
class Event:
    t: float
    kind: str                 # 'arm' | 'watch_recorded' | 'lane_touch' | 'reset'
    lane: int
    watch: Optional[str] = None


@dataclass
class LaneTracker:
    """Feed frames in order; get callbacks on meaningful transitions.

    Emits:
      * 'lane_touch'      first watch of a lane recording  (the touch moment)
      * 'watch_recorded'  every watch (A/B/C) recording a time
      * 'arm'             a lane going armed/running (gun)
      * 'reset'           a lane returning to idle
    """
    on_event: Optional[Callable[[Event], None]] = None
    _prev: Dict[Tuple[int, int], int] = field(default_factory=dict)
    _lane_touched: Dict[int, bool] = field(default_factory=dict)

    def feed(self, t: float, frame: bytes) -> None:
        if len(frame) < FRAME_LEN:
            return
        for lane in range(1, NUM_LANES + 1):
            lane_armed_now = False
            for w in range(3):
                st = frame[state_offset(lane, w)]
                key = (lane, w)
                prev = self._prev.get(key)
                if prev is not None and st != prev:
                    if st == S_RECORDED:
                        self._emit(Event(t, "watch_recorded", lane, WATCHES[w]))
                        if not self._lane_touched.get(lane):
                            self._lane_touched[lane] = True
                            self._emit(Event(t, "lane_touch", lane, WATCHES[w]))
                    elif st == S_RUNNING and prev == S_IDLE:
                        lane_armed_now = True
                    elif st == S_IDLE and prev != S_IDLE:
                        self._lane_touched[lane] = False
                        self._emit(Event(t, "reset", lane))
                self._prev[key] = st
            if lane_armed_now:
                self._emit(Event(t, "arm", lane))

    def _emit(self, ev: Event) -> None:
        if self.on_event:
            self.on_event(ev)


# --------------------------------------------------------------------------- #
# Frame sources                                                               #
# --------------------------------------------------------------------------- #
def frames_from_pcapng(path: str) -> Iterator[Tuple[float, bytes]]:
    """Offline: extract FTDI RX payloads via tshark and reassemble frames."""
    proc = subprocess.Popen(
        ["tshark", "-r", path, "-Y", "ftdi-ft.if_a_rx_payload",
         "-T", "fields", "-e", "frame.time_relative",
         "-e", "ftdi-ft.if_a_rx_payload"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def chunks() -> Iterator[Tuple[float, bytes]]:
        assert proc.stdout is not None
        for line in proc.stdout:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and parts[1]:
                yield float(parts[0]), bytes.fromhex(parts[1])

    yield from reassemble_frames(chunks())
    proc.wait()


def frames_from_pyshark(interface: str = "USBPcap1"
                        ) -> Iterator[Tuple[float, bytes]]:
    """Live: capture USBPcap traffic and reassemble frames. Requires pyshark."""
    import pyshark  # lazy import

    cap = pyshark.LiveCapture(
        interface=interface,
        display_filter=f"usb.idVendor==0x{FTDI_VID:04x} && ftdi-ft.if_a_rx_payload")

    def chunks() -> Iterator[Tuple[float, bytes]]:
        for pkt in cap.sniff_continuously():
            try:
                hexpay = pkt["ftdi-ft"].if_a_rx_payload.replace(":", "")
                yield float(pkt.sniff_timestamp), bytes.fromhex(hexpay)
            except (AttributeError, KeyError):
                continue

    yield from reassemble_frames(chunks())


# --------------------------------------------------------------------------- #
# Optional UDP emitter for the scoreboard                                     #
# --------------------------------------------------------------------------- #
class UDPEmitter:
    def __init__(self, host: str = "127.0.0.1", port: int = 6789) -> None:
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def __call__(self, ev: Event) -> None:
        msg = f"{ev.kind};{ev.lane};{ev.watch or ''};{ev.t:.3f}"
        self.sock.sendto(msg.encode(), self.addr)


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="CTS Dolphin USB finish decoder")
    ap.add_argument("pcapng", nargs="?", help="offline capture to decode")
    ap.add_argument("--live", metavar="IFACE",
                    help="live decode from a USBPcap interface, e.g. USBPcap1")
    ap.add_argument("--udp", metavar="HOST:PORT",
                    help="also emit events as UDP datagrams")
    args = ap.parse_args()

    emit_udp = None
    if args.udp:
        host, port = args.udp.split(":")
        emit_udp = UDPEmitter(host, int(port))

    def on_event(ev: Event) -> None:
        w = f" watch {ev.watch}" if ev.watch else ""
        print(f"  t={ev.t:7.2f}s  {ev.kind:14s} lane {ev.lane}{w}")
        if emit_udp:
            emit_udp(ev)

    tracker = LaneTracker(on_event=on_event)

    if args.live:
        print(f"Live decoding on {args.live} (Ctrl-C to stop)...")
        src = frames_from_pyshark(args.live)
    elif args.pcapng:
        print(f"Decoding {args.pcapng} ...")
        src = frames_from_pcapng(args.pcapng)
    else:
        ap.error("give a pcapng file or --live IFACE")
        return 2

    for t, frame in src:
        tracker.feed(t, frame)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
