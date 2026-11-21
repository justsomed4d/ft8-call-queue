# FT8 call-queue companion

A small attended companion for [WSJT-X](https://wsjt.sourceforge.io/wsjtx-doc/wsjtx-main-3.0.0.html) / JTDX / WS – Digital Mode Suite, meant to sit beside [GridTracker2](https://docs.gridtracker.org/latest/Making-GridTracker-Work-For-You/Using-Call-Roster.html).

You pick stations (from the companion's recent-CQ list, by typing a call, or optionally from a GridTracker alert script). When a queued station is decoded calling CQ or QRZ, the tool sends a WSJT-X UDP **Reply** (type 4) — the same action as double-clicking that decode. After the QSO is logged, or after a TX-turn timeout, it waits for the next queued station to call CQ.

Also built in:

- **Callback** — if a station calls you back, pick up the exchange and stay until it is logged.
- **Saved queue** — the queue is written to disk and restored if the companion restarts.
- **Clear queue** — one-click / `clear` command to empty the list.

This is a skeleton you can run and extend. It is **not** a GridTracker plugin; GridTracker2 has no extension API. It also does **not** start a QSO unless you turn **Arm** on.

## Rules of use

Stay at the radio. Under [47 CFR § 97.105](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-D/part-97/subpart-B/section-97.105) the control operator must ensure the immediate proper operation of the station. Unattended HF FT8 on the normal calling frequencies is [automatic control](https://www.law.cornell.edu/cfr/text/47/97.3), which [§ 97.221](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-D/part-97/subpart-C/section-97.221) does not allow there. [Parks on the Air](https://docs.pota.app/docs/rules.html) also prohibits fully automated QSOs. The Armed switch defaults to off; Halt Tx + disarm stops an in-progress call.

WSJT-X itself will not make consecutive fully automated QSOs: Auto Seq turns Enable Tx off at the end of each contact. This companion is what starts the *next* one, so treat Arm as a live control, not a set-and-forget switch.

## Requirements

- Python 3.10+ (stdlib only)
- WSJT-X 2.2+ or later (tested against the schema 3 UDP protocol used through WSJT-X 3.x), or JTDX / WS – Digital Mode Suite
- GridTracker2, if you want the roster and the optional alert hook

## WSJT-X settings

1. **File → Settings → Reporting**
   - Tick **Accept UDP requests** (required or Reply/HaltTx/Highlight are ignored).
   - Tick **Prompt me to log QSO** and **Clear DX call and grid after logging**.
   - Set **UDP Server** to multicast `224.0.0.73` port `2237` (GridTracker's documented example). Tick the loopback interface under **Outgoing interfaces** so local programs receive the group.
2. Main window: tick **Auto Seq**, tick **Hold Tx Freq**, and follow GridTracker's advice to untick **CQ only** if you also click non-CQ lines in the roster by hand.
3. **Disable Tx after sending 73** is recommended, as in the official guide.

## GridTracker2 settings

GridTracker2 and this companion must both hear WSJT-X. Unicast `127.0.0.1:2237` can only be bound by one program.

- Preferred: in GridTracker2, **Settings → General → Receive UDP Messages**, tick **Multicast?** and set IP `224.0.0.73`, port `2237`. Run this tool with the same group (the default).
- Alternative: leave WSJT-X on unicast to GridTracker2 and tick **Forward UDP Messages** to `127.0.0.1` and a second port, then run this tool as `--multicast '' --port <that port>`. Replies are sent back to the *source address of each datagram*. If forwarding rewrites the source so that it is GridTracker rather than WSJT-X, Reply will not work — use multicast if that happens.

This companion does **not** drive GridTracker's Call Roster. If the roster goes quiet after starting `queue.py`, both programs are probably fighting over unicast UDP — switch both (and WSJT-X) to multicast as above.

### Linux multicast notes

- **Outgoing interfaces** lists NICs such as `wlan0`. There is often **no `lo` / loopback** row; that is normal.
- If the field is left empty, WSJT-X may store `UDPInterface=@Invalid()` in `~/.config/WSJT-X.ini` and **send no UDP at all** (Band Activity still fills; GridTracker and this UI stay empty).
- On some Linux setups (observed on CachyOS), multicast published via **`wlan0` never loops back to local listeners**. Probes sent with `IP_MULTICAST_IF=lo` arrive; the same probe via `wlan0` does not. In that case picking `wlan0` will not feed GridTracker or this tool even though the ini looks correct.
- Workaround if `lo` is missing from the UI: quit WSJT-X, set `UDPInterface=lo` in `~/.config/WSJT-X.ini`, start WSJT-X again. Or use the unicast fallback below.
- GridTracker2 must enable multicast receive only when WSJT-X actually publishes multicast locally (`Settings → General → Receive UDP Messages` → **Multicast?** on, IP `224.0.0.73`, port `2237`).

### Fallback without multicast (recommended when local mcast loop fails)

1. WSJT-X: UDP Server `127.0.0.1`, port `2237` (Outgoing interfaces can be empty for unicast).
2. GridTracker2 **Receive UDP**: Multicast? **off**, port `2237`.
3. GridTracker2 **Forward UDP Messages**: Enabled → `127.0.0.1`, port `2238`.
4. Run this tool as: `python3 queue.py --multicast '' --port 2238`.

GridTracker2 has no "add to queue" button and no plugin API. Clicking a callsign in the Call Roster still tells WSJT-X to call them immediately, as it always did. To feed *wanted* alerts into this tool, see `cr-alert.example.sh`.

## Run

```bash
python3 queue.py --self-test          # pack/unpack check, no radio
python3 queue.py                      # multicast 224.0.0.73:2237, UI on 127.0.0.1:8765
```

Open http://127.0.0.1:8765/ — add calls, then click **Arm**.

Stdin commands: `add CALL`, `del CALL`, `up CALL`, `down CALL`, `clear`, `arm`, `disarm`, `halt`, `skip`, `tx-turns N`, `cq-ttl SECONDS`, `status`, `quit`.

Useful flags:

| Flag | Default | Meaning |
|------|---------|---------|
| `--timeout-cycles` | `3` | Unanswered TX turns before Halt (UI offers 1 / 3 / 5) |
| `--cq-ttl` | `600` | Drop a station from Recent CQs after N seconds without a CQ (`0` = never) |
| `--call-b4` | off | Allow calling a station already logged this session / today UTC |
| `--cty` | auto | Big CTY `cty.dat` for DXCC / CQ-zone labels (see below) |
| `--state` | `~/.cache/ft8-call-queue/queue.json` | Persist queue across restarts |
| `--no-state` | off | Do not load or save the queue |
| `--multicast` | `224.0.0.73` | Multicast group; use `''` for unicast |
| `--http` | `127.0.0.1:8765` | Web UI bind |

## Web UI

- **Queue** — callsigns you want answered on their next CQ/QRZ. Use **↑ / ↓** to reorder; when two queued stations are both calling, the higher entry wins (within ~45 s of their last CQ).
- **Clear queue** — button above the queue table (confirm prompt). Also available as stdin `clear` or `POST /api/queue/clear`. Clears active tracking; does not Halt Tx by itself.
- **Recent CQs** — one row per callsign (deduped), with age since last CQ (`12s`, `3m 5s`, …), a **DXCC** label from `cty.dat`, and a **queue** button. **Queued stations** controls how already-queued calls appear: none, highlight row, remove queue button, or both (default).
- **TX turns** — 1, 3, or 5 unanswered transmit periods before Halt. If WSJT-X drops Enable Tx after one call, the companion re-Replies on that station's next CQ until the budget is used.
- **Clear CQ after** — remove a station from Recent CQs when they have not called CQ within the chosen window (2 / 5 / 10 / 30 min, or never).
- **Hover tips** — controls, section headers, and table actions show a short summary on hover.
- **Help (?)** — fixed **?** button in the bottom-right opens a floating panel with this README (also `GET /api/help`). Escape or the backdrop closes it.

## Callback

When Armed and a decode is directed at your callsign (`YOURCALL DXCALL …`), the companion treats that as a callback: it sends a type-4 Reply (if TX is not already live), marks the exchange as answered, and stays with that station until a type-5 QSO Logged (TX-turn timeout does not abandon a callback). It will not interrupt an in-progress QSO with a different DX call. The UI shows `QSO with CALL (callback — until logged)` while that is active.

## Saved queue

The callsign list (order, notes, attempt counts) is saved after every add / remove / move / clear / timeout / log to `~/.cache/ft8-call-queue/queue.json` by default (`--state PATH` to override). On the next start the companion reloads that file and logs `restored N queued call(s)`. **Arm** is not restored — you must Arm again after a restart. Use `--no-state` to disable load and save.

## DXCC / CQ-zone labels (`cty.dat`)

WSJT-X paints country / zone text beside Band Activity using Big CTY. Those annotations are **not** in the UDP Decode packet. This companion loads the same file and looks up each Recent CQ callsign itself.

Search order: `--cty PATH`, then `~/.local/share/WSJT-X/cty.dat`, then `/usr/share/wsjtx/cty.dat`. v1 shows entity (e.g. Alaska, U.S.A., Argentina). US state strings and worked-before marks are out of scope.

## Logged today (UTC)

When a type-5 QSO Logged arrives while the companion is running, that callsign is:

- removed from the queue and from Recent CQs
- kept off Recent CQs for the rest of the **UTC calendar day** (resets at 00:00 UTC)
- skipped for auto-Reply unless you pass `--call-b4`

Only QSOs seen via UDP while this process is up are tracked — it does not read today's ADIF for earlier contacts.

## How it decides

1. A Decode directed at your callsign (callback) takes priority: Reply if needed and stay until QSO Logged.
2. Otherwise, a Decode whose text is CQ or QRZ and whose call is in the queue.
3. Armed is on, the radio is not already in a QSO with someone else, and the mode is not Fox/Hound.
4. If another queued station **above** this one has also CQ'd within the last ~45 s, skip this decode and wait for the higher-priority station (reorder with ↑ / ↓).
5. It echoes that decode back as a type-4 Reply, copying time, SNR, Δt, Δf, **mode marker**, message, and low-confidence exactly. The mode field is the Band Activity marker (`~` for FT8, `+` for FT4), not the string `FT8`.
6. Auto Seq runs the exchange. A type-5 QSO Logged, or N unanswered **TX turns** (callbacks exempt), clears that call and waits for the next queued CQ.
7. Queued calls are highlighted in WSJT-X's Band Activity window (type 13). The queue list is written to `--state` after every change.

Control messages are sent to the source address/port of the last datagram from that WSJT-X instance, which is required by the protocol.

## What this is not

It will not change band or mode, will not press Enable Tx to call CQ, and will not call a station that is mid-QSO with someone else (WSJT-X only accepts Reply for CQ/QRZ, or, in JTDX, whatever **Accept UDP Reply Message** allows). It is not a replacement for [WSJT-Z](https://github.com/sq9fve/wsjt-z) Auto Call or for WSJT-X 3.0 **Wait and Call** (one DX Call, up to three tries). Those existing options are discussed in the research write-up that accompanies this skeleton.
