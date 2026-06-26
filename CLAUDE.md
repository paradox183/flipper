# CLAUDE.md — CTS Dolphin Live Scoreboard

This file orients an AI assistant (and humans) working on this project. It
documents what the project is, the reverse-engineered protocol it depends on,
the architecture, and — importantly — *why* each design decision was made, so
that future changes don't silently undo hard-won fixes.

---

## 1. What this project is

A live scoreboard for swim meets timed with a **Colorado Time Systems (CTS)
Dolphin** wireless timing system. It reads timing data straight off the Dolphin
hardware and serves an auto-updating web page (default `http://localhost:8532`)
that shows, per lane: the three watch times (Timer A/B/C), the consolidated
result time, and a pool-converted result, plus a running race clock and the
current/next event and heat.

The meet runs **SwimTopia Meet Maestro** (file/CSV-based integration, not the
Dolphin socket) with a **CTS Infinity** electronic start. The scoreboard is a
read-only observer of the Dolphin; it must never disturb the live meet.

### Why it exists / the core insight
The Dolphin's TCP/IP socket only emits finished race **times** at *Reset* — too
late for a live board. The precise per-watch times are, however, present on the
**USB** stream the moment each watch is stopped. So the scoreboard takes times
from USB (fast, precise) and event/heat context from the TCP socket + a CSV.

---

## 2. Repository layout

- **`dolphin_scoreboard.py`** — the main, integrated application. This is what
  runs at a meet. Everything below describes it unless noted.
- **`dolphin_usb_decoder.py`** — an earlier standalone USB decoder focused on
  finish *detection*. Superseded by the scoreboard but kept as a minimal,
  readable reference for the framing/decoding in isolation.
- **`dolphin_client.py`** — a standalone, threaded TCP client for the Dolphin
  streaming socket (handshake, reconnect, read-only command guards, a capture
  mode that prints raw lines). Useful for probing the socket by hand.
- **`scoreboard.ini`** — runtime config (see §5). Auto-created on first run.
- **`Dolphin50.exe`** — the decompiled reference binary (CTS's own app). Not
  shipped/redistributed; kept locally as the authoritative source of the
  protocol. See §11.

Dependencies: **Python 3.8+ standard library only**, plus **tshark**
(Wireshark) at runtime to read the USB stream. No `pip install` step. This is
deliberate (see §10).

---

## 3. How to run

1. Install **Wireshark**, making sure the **USBPcap** component is checked
   during install (required to capture USB).
2. List capture interfaces and find the one the Dolphin base unit is on:
   `"C:\Program Files\Wireshark\tshark.exe" -D`
3. Edit `scoreboard.ini` (created on first run): set `usb_iface`,
   `dolphin_port`, the event-settings CSV path, and `conversion_factor`.
4. `python dolphin_scoreboard.py` (or pass a config path as the first arg).
5. Open `http://localhost:8532`.

On Windows, USBPcap capture sometimes requires running from an **Administrator**
prompt; an empty USB feed with the page's "USB" dot red is the usual symptom.

**Offline / no hardware:** set `usb_pcapng = <file>` and `no_tcp = true` in the
config to replay a capture through the full pipeline. This is how the decode is
regression-tested.

---

## 4. Architecture

Single process, several daemon threads, all sharing one lock-guarded `State`:

- **USB worker** — runs `tshark` (live on a USBPcap interface, or `-r` on a
  pcapng), pulls the `ftdi-ft.if_a_rx_payload` field, reassembles messages, and
  updates per-lane state.
- **TCP worker** — connects to the Dolphin streaming server, enables
  `autoSendUpdates`, polls `getCurrentEventAndHeat` every ~2 s, parses the
  current event/heat/race numbers.
- **CSV reloader** — re-reads the event-settings CSV when its mtime changes, so
  Meet Maestro updates are picked up mid-meet.
- **Clock thread** — runs the race-clock state machine at ~20 Hz (see §8).
- **HTTP server** (`ThreadingHTTPServer`) — serves `/` (the HTML page) and
  `/data` (a JSON snapshot). The page polls `/data` every 350 ms and
  extrapolates the running clock smoothly in the browser between polls.

Data flow: `tshark → USB worker → State.slots`; `socket → TCP worker →
State.live_event`; `CSV → State.event_map`; `clock thread → State race fields`.
The HTTP handler calls `State.snapshot()` to assemble the JSON the page renders.

---

## 5. Configuration (`scoreboard.ini`)

INI, single `[scoreboard]` section. Keys:

```
event_settings_folder    folder containing the event-settings CSV   (required)
event_settings_filename  the CSV filename                           (required)
usb_iface                USBPcap interface name, e.g. USBPcap1
dolphin_host             Dolphin TCP host (usually 127.0.0.1)
dolphin_port             Dolphin streaming port (Settings > Streaming)
lanes                    number of pool lanes
conversion_factor        pool conversion, 3 decimals (converted = result × factor)
http_port                web page port (default 8532)
# optional:
tshark_path              full path to tshark.exe (else auto-detected)
no_tcp                   true to disable the event/heat feed (USB times only)
usb_pcapng               replay a capture file instead of a live interface
```

A template is written automatically on first run, after which the app exits and
asks you to edit it.

---

## 6. The Dolphin USB protocol (reverse-engineered)

All of this was verified against captures **and** the decompiled binary
(23/23 known times reproduced exactly). Treat it as ground truth, but if a
future firmware revision breaks it, re-derive from §11.

### Transport
- Base unit is an **FTDI FT232R**, USB **VID:PID `0x0403:0x6001`**, bulk **IN
  endpoint `0x81`**.
- The Dolphin software opens it via **FTD2XX.dll (the D2XX driver)**, so there
  is **no COM port** to sniff — capture must happen at the USB layer (USBPcap).
- We read **passively** with tshark. tshark already strips the 2 FTDI status
  bytes and exposes the payload as `ftdi-ft.if_a_rx_payload`.

### Framing
Messages are delimited by the **high bit (`0x80`) of the first byte**: a byte
with `0x80` set starts a new message; all body bytes have `0x80` clear. This is
why the time value is 7-bit packed (the MSB is reserved as a frame marker), and
it is **firmware-version independent**. Each message is **11 bytes**. (The
"~352-byte status frame" some notes mention is just ~32 of these concatenated.)

### Timer-message layout
```
byte0 : 0x80 | state          state = byte0 & 0x7F     (see MessageState below)
byte1 : slot                  lane  = (slot - 2)//3 + 1
                              watch = (slot - 2)%3      (0=A, 1=B, 2=C)
byte2 : splitCount  (& 0x7F)
byte3 : battery     (capped at 100)
byte4 : signal      (capped at 100)   <- this is the recurring 0x64 = 100%
byte5..9 : time_ms = (b5<<25)|(b6<<18)|(b7<<11)|(b8<<4)|(b9 & 0x0F)   # 7-bit packed, MILLISECONDS

displayed_hundredths = (time_ms + 5) // 10     # ms -> centiseconds, rounded
```

### `MessageState` enum (from `DolphinInterface.MessageState`)
```
Config = 0     Stop = 1     Reset = 2     Run = 3     Program = 4
```
**This is the single most important fact in the project.** A real, manually
recorded time exists **only in a `Stop` (1) message**. `Reset` (2) is the race
being reset — and crucially, watches the timer never stopped are assigned the
**reset moment's time** in a `Reset`-state message. Those must be ignored. `Run`
(3) is the watch counting during a race. (An early version mislabeled state 2 as
"armed-with-time" and accepted its time, which caused both the un-stopped-watch
bug and the clock-never-stops bug — see §9.)

### Final-time consolidation (`LaneTime.calcSplitFinalTimes`)
Combine the per-watch times for a lane exactly as the Dolphin does:
- 1 watch → that time
- 2 watches → `(a + b + 1) // 2` (average, rounded)
- 3 watches → **median** (the middle value)

### There is no "correction factor"
A long investigation chased a supposed time-correction factor. There isn't one:
`timeInHundredths = (time_ms + 5)/10` is just a millisecond→centisecond unit
conversion. The precise time is fully present on USB (it had appeared otherwise
only because the value is 7-bit packed and lives in a discrete timer message,
not the periodic status sweep).

---

## 7. The Dolphin TCP streaming protocol

- The **Dolphin software is the TCP server**; our app connects as a client. The
  port is user-configured under **Settings (F4) > Streaming**.
- Commands are comma-delimited text lines, CRLF-terminated.
- We use only: `autoSendUpdates,ON` and `getCurrentEventAndHeat`.
- `getCurrentEventAndHeat` reply:
  `(version),(eventIndex),(eventNumber),(heatNumber),(raceNumber);`
  We parse the trailing four integers (index, number, heat, race).
- Pushed-update line prefixes seen: `CurrentEventAndHeat,` and `EventChanged,`.
- **READ-ONLY INVARIANT:** never send mutating commands
  (`clearEvents`, `setEventInfo`, `setCurrentEventAndHeat`, `Reset`, …). They
  would alter the live meet / Meet Maestro state.

Event **names** are *not* taken from the socket — see §8 for why.

---

## 8. The event-settings CSV (names + heat counts)

Configured via `event_settings_folder` + `event_settings_filename` (separate
keys, joined into a path). Exported by Meet Maestro. Column order:

```
event number (int), event name (string), number of heats (int), <ignored>, <ignored>
```

The parser skips any header/non-data rows (rows whose 1st/3rd columns aren't
integers) and ignores columns past the third. Event names may contain `&` and
other markup-significant characters; the page injects them via `textContent`, so
they are HTML-escaped automatically — never switch the name to `innerHTML`.

The CSV provides two things: the **current event name** (lookup by current event
number) and the **heat count per event**, used to compute whether the *next*
heat is the same event (`heat + 1`) or the next event (`heat == numHeats` →
`event + 1`, heat 1).

---

## 9. Race-clock state machine

A *local* clock (not synchronized to the watches — by design, see §10). Lives in
`State`, driven by the 20 Hz clock thread reading the USB lane states:

- **Gun:** first time any watch is in `Run` (3) while not already running →
  `race_running = True`, capture `race_t0 = monotonic()`.
- **Reset:** while running, any watch in `Reset` (2) and none in `Run` →
  `race_running = False`, freeze the elapsed value.
- The clock shows `now - race_t0` while running, else the frozen value. The
  **next gun** re-anchors `race_t0`, so it restarts from zero.

The page renders the clock each animation frame, extrapolating from the last
`/data` snapshot, so it ticks smoothly in hundredths between 350 ms polls.

---

## 10. Event/heat "hold on reset" + next-heat preview

Problem: the Dolphin **auto-advances the heat on Reset**. If the board followed
the live socket value, the current event/heat would jump ahead the instant a
race ends, which is confusing.

Solution — anchor the *displayed* current event/heat to the **gun**, not to the
socket:
- `shown_event` is captured at each gun edge (and tracks the live value *before*
  the first race so the upcoming heat is visible during setup).
- After the first gun, `shown_event` changes **only** at subsequent gun edges.
  So when the Dolphin resets and advances, the board keeps showing the heat that
  just ran, and resumes the live value at the next gun.
- `between_races` is set on the Reset edge and cleared on the gun edge. While
  it's set, the page shows the italic **NEXT HEAT** table (top-right), whose
  value is **computed from the CSV** (`shown_event` + heat counts), not read
  from the live socket.

Why gun-anchored and CSV-computed: the heat advance (TCP) and the reset signal
(USB) both happen "at reset" with **unpredictable ordering**. Anchoring to the
gun — an unambiguous, well-separated event — makes the held value immune to that
race condition, and computing "next" from the CSV makes the preview deterministic
regardless of socket timing.

---

## 11. Design decisions and rationale

**USB for times, not the socket.** The socket only emits race times at Reset.
USB timer messages arrive within ~one frame (~290 ms) of each touch, which is
what a live board needs.

**Passive USBPcap capture via tshark, not pyusb/D2XX.** The Dolphin software
holds the FTDI device open through the D2XX driver; a second opener (pyusb)
would conflict and could disrupt timing. Passive capture is non-invasive — it
cannot disturb the Dolphin software or Meet Maestro. tshark also already strips
FTDI status bytes, and reusing it for both live and offline (`-i` vs `-r`) keeps
one code path.

**`0x80`-bit framing, not fixed-length/firmware-string anchoring.** An earlier
version found message boundaries by anchoring on the firmware version string
`"1.51"`, which breaks on any firmware update. The high-bit framing is
byte-level and firmware-independent.

**Latch times only from `Stop` (1).** This is the fix for the un-stopped-watch
bug. The decompiled `MessageState` enum showed state 2 is `Reset`, not
"armed-with-time"; only `Stop` carries a real manual time. A watch that goes
`Run → Reset` without a `Stop` correctly shows **no** time.

**Consolidation copied verbatim from the binary.** Median/average/single is
lifted from `LaneTime.calcSplitFinalTimes` so the board's finals match the
official Dolphin output exactly, rather than approximating.

**Local, gun-anchored race clock (not synced to the watches).** Per the
requirement, the clock only needs to start with the gun and run in parallel; the
precise times already come from USB. A local monotonic clock is simple and
robust, and browser-side extrapolation keeps it visually smooth without
hammering the server.

**Reset detected from the `Reset` state, not from "no finals."** The original
clock logic inferred reset from the absence of finals, which never fired because
reset assigns times to un-stopped watches (finals never disappeared). Using the
explicit `Reset` state fixes both the clock-stop and the next-race-zero behavior.

**Event/heat gun-anchored and held on reset; next-heat from CSV.** See §10 — it
avoids the confusing auto-advance and is immune to TCP-vs-USB timing races.

**Event names from the CSV, not the socket.** The socket's `getEventInfo` /
`EventChanged` response format could never be confirmed reliably from the binary,
so depending on it was fragile. The Meet Maestro CSV is a deterministic source of
names *and* the heat counts needed for the next-heat logic.

**Conversion factor in the config, not the GUI.** It's a per-pool setting set
once; it shouldn't be casually editable on a live board. Converted time is
computed from the precise underlying milliseconds (`round(final_ms × factor)`),
which is slightly sharper than multiplying a rounded display value.

**Config file instead of CLI flags.** Simpler for non-developers to set up
poolside, one place for all settings, and a template is auto-written on first
run. A config path may still be passed as the first CLI argument.

**Web page (HTTP + polling) instead of a native GUI.** A browser board can go
fullscreen on any monitor/projector/Chromecast and be viewed from several
devices at once, with no GUI toolkit. `ThreadingHTTPServer` + a lock-guarded
`State` lets multiple clients poll concurrently. 350 ms polling with browser-side
clock extrapolation looks live while staying on the stdlib.

**Standard library only (+ tshark).** Meet-day machines shouldn't need a Python
package install. tshark is already required for USB, so it's the one external
dependency.

**tshark auto-detection.** tshark isn't on `PATH` by default on Windows (the
classic `[WinError 2]`), so the app checks the standard Wireshark install
locations and allows a `tshark_path` override.

**Read-only on the socket.** Never send mutating commands; the meet's source of
truth is Meet Maestro and the Dolphin operator.

---

## 12. Known limitations & unverified assumptions

- **Un-stopped-watch filtering** is verified in simulation and via the corrected
  state semantics, but not yet against a live capture of a watch left running
  through a reset. If a stray time ever appears for an unstopped watch, capture
  it and check the state bytes.
- **CSV column order** is assumed as documented in §8 (header auto-skipped,
  columns past 3 ignored). Confirm against a real Meet Maestro export.
- **Lane mapping** assumes no "lane 0" (the Dolphin `StartWithLaneZero` option).
  slot→lane was verified on real captures for lanes 1–6 and extrapolated to
  7–10 (lane 10's last record ends at byte 348, inside the frame).
- **TCP-poll vs gun timing:** the current heat captured at the gun depends on the
  socket value being fresh (polled every ~2 s). With normal between-race setup
  time this is fine; if the displayed current heat is ever one off, it's poll
  timing.
- **Live clock / live TCP** are not testable without hardware; they're validated
  via offline pcapng replay and direct `State` unit simulations.
- The timer-message **field layout** is firmware-independent in framing but could
  in principle change in a major firmware revision; re-derive from §11/§13.

---

## 13. The reference binary & re-deriving the protocol

`Dolphin.exe` shipped is only a launcher (`Dolphin5Launcher`) that starts
`C:\Program Files (x86)\Colorado Time Systems\Dolphin5\Dolphin50.exe`. The real
app is **`Dolphin50.exe`** — **.NET Framework 4.8, not obfuscated**.

To re-inspect (Linux): `monodis Dolphin50.exe > dolphin50.il`, then read the IL.
(ILSpy works too if a .NET SDK is available.) Key types/methods:
- `DolphinInterface.MessageState` — the enum in §6.
- `TimerMessage` — fields `state, battery, signal, lane, timerNumber, time,
  timeInHundredths, splitCount, off, error`.
- `RawDataToObject` — `Marshal.PtrToStructure` overlay of the raw bytes onto the
  struct (this is where the field offsets come from).
- `LaneTime.calcFinalTimes` / `calcSplitFinalTimes` — the consolidation in §6.
- `processTimerMessage`, `processBytesRead` — message routing / byte framing.

---

## 14. Testing

- **Offline replay:** `usb_pcapng = <capture>` + `no_tcp = true` runs a capture
  through the real pipeline; hit `/data` and check the lanes.
- **Unit simulation:** import the module and drive `State` directly
  (`update_slot`, `update_event`, `tick_clock`, `snapshot`) to test the clock,
  hold-on-reset, and next-heat logic without hardware.
- **Verification corpus** (captures used to lock the decode): a single-watch
  9.81 s race; a 5-race calibration set with known finals (5.20, 10.24, 30.05,
  60.73, 215.15 s); and a 6-lane × 3-watch test race whose medians match the
  Dolphin's own result file. Any change to the decode should still reproduce
  these exactly.

---

## 15. Invariants — do not break these

1. **Never send mutating TCP commands.** Read-only on the socket (§7).
2. **Never open the FTDI device directly.** Passive capture only (§11).
3. **A lane time is recorded only from a `Stop`-state message** (§6, §9).
4. **Match the Dolphin's own consolidation math** (median / average / single).
5. **Event names render via `textContent`**, never `innerHTML` (§8).
6. Keep the **stdlib-only (+ tshark)** footprint unless there's a strong reason.
