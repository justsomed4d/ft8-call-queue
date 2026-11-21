#!/usr/bin/env python3
"""FT8 call-queue companion for WSJT-X / JTDX (attended use only).

Listens to the WSJT-X UDP protocol (schema 3, Qt_5_4 QDataStream), lets you
queue callsigns, and when a queued station is decoded calling CQ or QRZ,
sends a type-4 Reply — the same action as double-clicking that decode.

This is an operator aid. Keep the control operator at the control point, with
the Armed switch off unless you are present and ready to Halt Tx. See README.md.
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import socket
import struct
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

README_PATH = Path(__file__).resolve().with_name("README.md")

MAGIC = 0xADBCCBDA
SCHEMA = 3  # Qt_5_4, as used by WSJT-X 2.1 through 3.x
UTF8_NULL = 0xFFFFFFFF

# NetworkMessage.hpp Type enum (do not renumber)
HEARTBEAT = 0
STATUS = 1
DECODE = 2
CLEAR = 3
REPLY = 4
QSO_LOGGED = 5
CLOSE = 6
REPLAY = 7
HALT_TX = 8
FREE_TEXT = 9
HIGHLIGHT_CALLSIGN = 13

# Reply keyboard modifiers (NetworkMessage.hpp, hex)
MOD_NONE = 0x00
MOD_SHIFT = 0x02
MOD_CTRL = 0x04
MOD_ALT = 0x08

# Decode "Mode" is the Band Activity marker, NOT "FT8"/"FT4".
# Sending "FT8" in a Reply is silently ignored. Confirmed against the
# official header and a 2025 WSJT-X Improved thread on type-4 format.
MODE_MARKERS = {
    "~": "FT8",
    "+": "FT4",
    "#": "JT65",
    "@": "JT9",
    ":": "FST4/Q65",
    "&": "MSK144",
}

# Special Operation Mode in Status (current official NetworkMessage.hpp)
SPECIAL_OP = {
    0: "NONE",
    1: "NA VHF",
    2: "EU VHF",
    3: "FIELD DAY",
    4: "RTTY RU",
    5: "WW DIGI",
    6: "FOX",
    7: "HOUND",
    8: "ARRL DIGI",
}

CALL_RE = re.compile(
    r"^[A-Z0-9]{1,3}[0-9][A-Z0-9]{0,4}(?:/[A-Z0-9]+){0,2}$",
    re.IGNORECASE,
)
GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}(?:[A-X]{2})?$", re.IGNORECASE)

# Messages directed at us: "KF0QGN W1ABC -10", "KF0QGN W1ABC R-08", etc.
DIRECTED_RE = re.compile(
    r"^(?P<tocall>[A-Z0-9/]+)\s+(?P<fromcall>[A-Z0-9/]+)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# QDataStream (big-endian, Qt_5_4) helpers
# ---------------------------------------------------------------------------

class PacketError(ValueError):
    pass


class Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.o = 0

    def remaining(self) -> int:
        return len(self.data) - self.o

    def _need(self, n: int) -> None:
        if self.remaining() < n:
            raise PacketError(f"truncated packet (need {n}, have {self.remaining()})")

    def u8(self) -> int:
        self._need(1)
        v = self.data[self.o]
        self.o += 1
        return v

    def bool(self) -> bool:
        return bool(self.u8())

    def u16(self) -> int:
        self._need(2)
        v = struct.unpack_from(">H", self.data, self.o)[0]
        self.o += 2
        return v

    def u32(self) -> int:
        self._need(4)
        v = struct.unpack_from(">I", self.data, self.o)[0]
        self.o += 4
        return v

    def i32(self) -> int:
        self._need(4)
        v = struct.unpack_from(">i", self.data, self.o)[0]
        self.o += 4
        return v

    def u64(self) -> int:
        self._need(8)
        v = struct.unpack_from(">Q", self.data, self.o)[0]
        self.o += 8
        return v

    def f64(self) -> float:
        self._need(8)
        v = struct.unpack_from(">d", self.data, self.o)[0]
        self.o += 8
        return v

    def utf8(self) -> str:
        n = self.u32()
        if n == UTF8_NULL:
            return ""
        self._need(n)
        s = self.data[self.o : self.o + n].decode("utf-8", errors="replace")
        self.o += n
        return s

    def qdatetime(self) -> None:
        """Skip a QDateTime (Julian day + QTime + timespec [+ offset/tz])."""
        self.u64()  # QDate: qint64 Julian day
        self.u32()  # QTime: quint32 ms since midnight
        spec = self.u8()
        if spec == 2:
            self.i32()  # offset seconds
        elif spec == 3:
            # timezone: several fields; skip what's left of a typical Qt tz
            # We only need QSO-logged DX call, so callers should read that
            # after this. If a packet uses timespec=3, parsing may fail;
            # WSJT-X documents that it avoids time-zone QDateTimes.
            pass


class Writer:
    def __init__(self):
        self.buf = bytearray()

    def u8(self, v: int) -> None:
        self.buf.append(v & 0xFF)

    def bool(self, v: bool) -> None:
        self.u8(1 if v else 0)

    def u16(self, v: int) -> None:
        self.buf += struct.pack(">H", v & 0xFFFF)

    def u32(self, v: int) -> None:
        self.buf += struct.pack(">I", v & 0xFFFFFFFF)

    def i32(self, v: int) -> None:
        self.buf += struct.pack(">i", int(v))

    def f64(self, v: float) -> None:
        self.buf += struct.pack(">d", float(v))

    def utf8(self, s: str) -> None:
        if s is None:
            self.u32(UTF8_NULL)
            return
        b = s.encode("utf-8")
        self.u32(len(b))
        self.buf += b

    def header(self, typ: int, ident: str) -> None:
        self.u32(MAGIC)
        self.u32(SCHEMA)
        self.u32(typ)
        self.utf8(ident)

    def bytes(self) -> bytes:
        return bytes(self.buf)


def pack_reply(
    ident: str,
    time_ms: int,
    snr: int,
    dt: float,
    df: int,
    mode: str,
    message: str,
    low_confidence: bool,
    modifiers: int = MOD_NONE,
) -> bytes:
    """Type 4 Reply. Field order matches NetworkMessage.hpp exactly.

    time, snr, dt, df, mode, message, low_confidence must be copied from the
    Decode you are answering — WSJT-X only acts if they describe that decode
    and the text is a CQ or QRZ.
    """
    w = Writer()
    w.header(REPLY, ident)
    w.u32(time_ms)  # QTime = quint32 milliseconds since midnight
    w.i32(snr)
    w.f64(dt)
    w.u32(df)
    w.utf8(mode)
    w.utf8(message)
    w.bool(low_confidence)
    w.u8(modifiers)
    return w.bytes()


def pack_halt_tx(ident: str, auto_tx_only: bool = False) -> bytes:
    w = Writer()
    w.header(HALT_TX, ident)
    w.bool(auto_tx_only)
    return w.bytes()


def pack_highlight(
    ident: str,
    callsign: str,
    bg: Optional[tuple[int, int, int]] = (255, 200, 0),
    fg: Optional[tuple[int, int, int]] = (0, 0, 0),
    highlight_last: bool = False,
) -> bytes:
    """Type 13 HighlightCallsign. QColor is spec + 5 x quint16 (a, r, g, b, pad).

    spec 1 = RGB; spec 0 = invalid (clears highlighting). 8-bit components
    are written as v * 0x0101 (255 -> 0xFFFF), per Qt QDataStream.
    """
    w = Writer()
    w.header(HIGHLIGHT_CALLSIGN, ident)
    w.utf8(callsign)
    _write_qcolor(w, bg)
    _write_qcolor(w, fg)
    w.bool(highlight_last)
    return w.bytes()


def _write_qcolor(w: Writer, rgb: Optional[tuple[int, int, int]]) -> None:
    if rgb is None:
        w.u8(0)  # QColor::Invalid
        w.u16(0)
        w.u16(0)
        w.u16(0)
        w.u16(0)
        w.u16(0)
        return
    r, g, b = rgb
    w.u8(1)  # QColor::Rgb
    w.u16(0xFFFF)  # alpha
    w.u16((r & 0xFF) * 0x0101)
    w.u16((g & 0xFF) * 0x0101)
    w.u16((b & 0xFF) * 0x0101)
    w.u16(0)  # pad


def parse_header(data: bytes) -> tuple[int, int, str, Reader]:
    r = Reader(data)
    magic = r.u32()
    if magic != MAGIC:
        raise PacketError(f"bad magic 0x{magic:08x}")
    schema = r.u32()
    typ = r.u32()
    ident = r.utf8()
    return schema, typ, ident, r


def parse_decode(r: Reader) -> dict[str, Any]:
    return {
        "new": r.bool(),
        "time": r.u32(),
        "snr": r.i32(),
        "dt": r.f64(),
        "df": r.u32(),
        "mode": r.utf8(),
        "message": r.utf8(),
        "low_confidence": r.bool(),
        "off_air": r.bool() if r.remaining() else False,
    }


def parse_status(r: Reader) -> dict[str, Any]:
    # Current official field list; trailing fields may be absent on old clients.
    out: dict[str, Any] = {
        "dial_freq": r.u64(),
        "mode": r.utf8(),
        "dx_call": r.utf8(),
        "report": r.utf8(),
        "tx_mode": r.utf8(),
        "tx_enabled": r.bool(),
        "transmitting": r.bool(),
        "decoding": r.bool(),
        "rx_df": r.u32(),
        "tx_df": r.u32(),
        "de_call": r.utf8(),
        "de_grid": r.utf8(),
        "dx_grid": r.utf8(),
        "tx_watchdog": r.bool(),
        "sub_mode": r.utf8(),
        "fast_mode": r.bool(),
        "special_op": r.u8(),
    }
    if r.remaining() >= 4:
        out["freq_tol"] = r.u32()
    if r.remaining() >= 4:
        out["tr_period"] = r.u32()
    if r.remaining() >= 4:
        out["config_name"] = r.utf8()
    if r.remaining() >= 4:
        out["tx_message"] = r.utf8()
    return out


def parse_qso_logged(r: Reader) -> dict[str, Any]:
    r.qdatetime()  # datetime off
    dx_call = r.utf8()
    dx_grid = r.utf8()
    tx_freq = r.u64()
    mode = r.utf8()
    report_sent = r.utf8()
    report_rcvd = r.utf8()
    # remaining fields unused by the queue
    return {
        "dx_call": dx_call,
        "dx_grid": dx_grid,
        "tx_freq": tx_freq,
        "mode": mode,
        "report_sent": report_sent,
        "report_rcvd": report_rcvd,
    }


def parse_cq(message: str) -> Optional[dict[str, str]]:
    """Pull CQ/QRZ, optional directive, call, optional grid out of a decode.

    Tokenize from the right so a directive like DX/POTA is not mistaken for
    the callsign. Trailing decoder flags such as '?' or 'a1' are ignored.
    """
    text = message.strip()
    text = re.sub(r"\s+[a-z]?\d?\??$", "", text)
    parts = text.split()
    if len(parts) < 2 or parts[0].upper() not in ("CQ", "QRZ"):
        return None
    grid = ""
    if GRID_RE.fullmatch(parts[-1]):
        grid = parts[-1].upper()
        parts = parts[:-1]
    if len(parts) < 2 or not CALL_RE.fullmatch(parts[-1]):
        return None
    return {
        "call": parts[-1].upper(),
        "dir": " ".join(parts[1:-1]).upper(),
        "grid": grid,
    }


def hz_to_band(hz: int) -> str:
    mhz = hz / 1e6
    bands = (
        (1.8, 2.0, "160m"),
        (3.5, 4.0, "80m"),
        (5.3, 5.5, "60m"),
        (7.0, 7.3, "40m"),
        (10.1, 10.15, "30m"),
        (14.0, 14.35, "20m"),
        (18.068, 18.168, "17m"),
        (21.0, 21.45, "15m"),
        (24.89, 24.99, "12m"),
        (28.0, 29.7, "10m"),
        (50.0, 54.0, "6m"),
        (144.0, 148.0, "2m"),
    )
    for lo, hi, name in bands:
        if lo <= mhz <= hi:
            return name
    return f"{mhz:.3f} MHz"


# ---------------------------------------------------------------------------
# Big CTY (cty.dat) — same file WSJT-X uses for DXCC / CQ-zone annotations
# ---------------------------------------------------------------------------

CTY_HEADER_RE = re.compile(
    r"^(?P<name>.+?):\s*"
    r"(?P<cq>\d+):\s*"
    r"(?P<itu>\d+):\s*"
    r"(?P<cont>\S+):\s*"
    r"(?P<lat>[-\d.]+):\s*"
    r"(?P<lon>[-\d.]+):\s*"
    r"(?P<tz>[-\d.]+):\s*"
    r"(?P<pfx>\S+):\s*$"
)
CTY_TOKEN_RE = re.compile(
    r"^(?P<exact>=)?(?P<body>[A-Z0-9/]+?)(?:\((?P<cq>\d+)\))?(?:\[(?P<itu>\d+)\])?$",
    re.IGNORECASE,
)
ENTITY_SHORT = {
    "United States": "U.S.A.",
    "Asiatic Russia": "Asiatic Russia",
    "European Russia": "European Russia",
    "Fed. Rep. of Germany": "Germany",
    "Republic of Korea": "South Korea",
}

# How Recent CQs mark callsigns that are already in the queue.
QUEUED_CQ_STYLES = ("none", "highlight", "hide_btn", "both")

# When two+ queued stations are calling CQ, prefer the higher list entry.
# A higher entry only blocks lower ones if it CQ'd within this window.
PRIORITY_FRESH_S = 45

DEFAULT_STATE_PATH = Path.home() / ".cache" / "ft8-call-queue" / "queue.json"


@dataclass(frozen=True)
class CtyHit:
    entity: str
    cq_zone: str
    itu_zone: str
    continent: str
    pfx: str

    @property
    def label(self) -> str:
        name = ENTITY_SHORT.get(self.entity, self.entity)
        # Compact WSJT-X-like right-hand text: entity, with zone when useful.
        if name in ("U.S.A.", "Canada", "Alaska", "Hawaii"):
            return name
        return f"{name}"


class CtyDB:
    """Prefix / exact-call lookup against a Big CTY `cty.dat` file."""

    def __init__(self, path: Path):
        self.path = path
        self.exact: dict[str, CtyHit] = {}
        self.prefixes: list[tuple[str, CtyHit]] = []  # longest first
        self._load(path)

    def _load(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8", errors="replace")
        exact: dict[str, CtyHit] = {}
        prefixes: list[tuple[str, CtyHit]] = []
        entity: Optional[dict[str, str]] = None
        buf: list[str] = []

        def flush() -> None:
            nonlocal entity, buf
            if entity is None:
                buf = []
                return
            blob = "".join(buf)
            buf = []
            for raw in blob.split(","):
                tok = raw.strip().rstrip(";").strip()
                if not tok:
                    continue
                m = CTY_TOKEN_RE.fullmatch(tok)
                if not m:
                    continue
                body = m.group("body").upper()
                hit = CtyHit(
                    entity=entity["name"],
                    cq_zone=m.group("cq") or entity["cq"],
                    itu_zone=m.group("itu") or entity["itu"],
                    continent=entity["cont"],
                    pfx=entity["pfx"],
                )
                if m.group("exact"):
                    exact[body] = hit
                else:
                    prefixes.append((body, hit))
            entity = None

        for line in text.splitlines():
            if entity is None:
                hm = CTY_HEADER_RE.match(line.strip())
                if not hm:
                    continue
                entity = {
                    "name": hm.group("name").strip(),
                    "cq": str(int(hm.group("cq"))),
                    "itu": str(int(hm.group("itu"))),
                    "cont": hm.group("cont"),
                    "pfx": hm.group("pfx").rstrip(":"),
                }
                buf = []
                continue
            buf.append(line)
            if ";" in line:
                flush()
        flush()

        prefixes.sort(key=lambda x: len(x[0]), reverse=True)
        self.exact = exact
        self.prefixes = prefixes

    def lookup(self, call: str) -> Optional[CtyHit]:
        call = (call or "").strip().upper()
        if not call:
            return None
        # Exact match on full call, then on each slash segment (CALL/P, KH6/W1ABC).
        parts = [call, *call.split("/")]
        seen: set[str] = set()
        for part in parts:
            if not part or part in seen:
                continue
            seen.add(part)
            hit = self.exact.get(part)
            if hit:
                return hit
        # Longest prefix against the home call (segment before first /).
        base = call.split("/", 1)[0]
        for pfx, hit in self.prefixes:
            if base.startswith(pfx):
                return hit
        return None

    def annotate(self, call: str) -> dict[str, str]:
        hit = self.lookup(call)
        if not hit:
            return {}
        return {
            "entity": hit.entity,
            "cq_zone": hit.cq_zone,
            "itu_zone": hit.itu_zone,
            "continent": hit.continent,
            "pfx": hit.pfx,
            "label": hit.label,
        }


def resolve_cty_path(explicit: Optional[str] = None) -> Optional[Path]:
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    for p in (
        Path.home() / ".local/share/WSJT-X/cty.dat",
        Path("/usr/share/wsjtx/cty.dat"),
        Path("/usr/local/share/wsjtx/cty.dat"),
    ):
        if p.is_file():
            return p
    return None


def load_cty(explicit: Optional[str] = None) -> Optional[CtyDB]:
    path = resolve_cty_path(explicit)
    if not path:
        return None
    try:
        return CtyDB(path)
    except OSError as exc:
        print(f"cty.dat load failed ({path}): {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Queue state machine
# ---------------------------------------------------------------------------

@dataclass
class QueueItem:
    call: str
    added: float = field(default_factory=time.time)
    note: str = ""
    attempts: int = 0


@dataclass
class DecodeSnap:
    ident: str
    time: int
    snr: int
    dt: float
    df: int
    mode: str
    message: str
    low_confidence: bool


class CallQueue:
    def __init__(
        self,
        timeout_cycles: int = 3,
        skip_b4: bool = True,
        cq_ttl: int = 600,
        cty: Optional[CtyDB] = None,
        state_path: Optional[Path] = None,
    ):
        self.lock = threading.Lock()
        self.queue: list[QueueItem] = []
        self.armed = False
        # Unanswered TX periods before Halt (UI offers 1 / 3 / 5).
        self.timeout_cycles = max(1, int(timeout_cycles))
        self.skip_b4 = skip_b4
        self.cq_ttl = max(0, int(cq_ttl))  # seconds; 0 = never expire
        self.queued_cq_style = "both"  # none | highlight | hide_btn | both
        self.cty = cty
        self.state_path = Path(state_path) if state_path else None
        self.worked: set[tuple[str, str, str]] = set()  # (call, band, mode)
        self.logged_today: set[str] = set()  # callsigns logged this UTC day
        self.logged_day: str = time.strftime("%Y-%m-%d", time.gmtime())
        self.status: dict[str, Any] = {}
        self.ident = "WSJT-X"
        self.peer: Optional[tuple[str, int]] = None
        self.sock: Optional[socket.socket] = None
        self.active: Optional[str] = None
        self.active_started: float = 0.0
        self.cycles_on_active = 0
        self.heard_reply = False
        self.log: list[str] = []
        self.recent_cqs: list[dict[str, Any]] = []  # one entry per callsign
        self._last_decode_period = 0.0

    def event(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        self.log.append(line)
        if len(self.log) > 200:
            self.log = self.log[-200:]
        print(line, flush=True)

    def _save_locked(self) -> None:
        if not self.state_path:
            return
        data = {
            "version": 1,
            "queue": [
                {
                    "call": i.call,
                    "note": i.note,
                    "attempts": i.attempts,
                    "added": i.added,
                }
                for i in self.queue
            ],
        }
        path = self.state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            self.event(f"queue save failed: {exc}")

    def load_state(self) -> int:
        """Load persisted queue. Returns number of restored callsigns."""
        if not self.state_path or not self.state_path.is_file():
            return 0
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.event(f"queue load failed: {exc}")
            return 0
        items = data.get("queue") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return 0
        restored: list[QueueItem] = []
        seen: set[str] = set()
        for raw in items:
            if not isinstance(raw, dict):
                continue
            call = str(raw.get("call") or "").strip().upper()
            if not call or call in seen:
                continue
            seen.add(call)
            try:
                added = float(raw.get("added") or time.time())
            except (TypeError, ValueError):
                added = time.time()
            try:
                attempts = int(raw.get("attempts") or 0)
            except (TypeError, ValueError):
                attempts = 0
            restored.append(
                QueueItem(
                    call=call,
                    note=str(raw.get("note") or ""),
                    attempts=max(0, attempts),
                    added=added,
                )
            )
        with self.lock:
            self.queue = restored
            n = len(self.queue)
        if n:
            self.event(f"restored {n} queued call(s) from {self.state_path}")
        return n

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            self._prune_cqs_locked()
            now = time.time()
            st = dict(self.status)
            queued_calls = {i.call for i in self.queue}
            cqs = []
            for e in self.recent_cqs:
                row = dict(e)
                row["age_s"] = max(0, int(now - e["t"]))
                row["queued"] = e["call"] in queued_calls
                cqs.append(row)
            return {
                "armed": self.armed,
                "ident": self.ident,
                "peer": list(self.peer) if self.peer else None,
                "status": {
                    "dial_freq": st.get("dial_freq"),
                    "band": hz_to_band(st["dial_freq"]) if st.get("dial_freq") else "",
                    "mode": st.get("mode"),
                    "dx_call": st.get("dx_call"),
                    "de_call": st.get("de_call"),
                    "tx_enabled": st.get("tx_enabled"),
                    "transmitting": st.get("transmitting"),
                    "tx_message": st.get("tx_message"),
                    "special_op": SPECIAL_OP.get(st.get("special_op", 0), st.get("special_op")),
                },
                "active": self.active,
                "cycles_on_active": self.cycles_on_active,
                "timeout_cycles": self.timeout_cycles,
                "cq_ttl": self.cq_ttl,
                "queued_cq_style": self.queued_cq_style,
                "heard_reply": self.heard_reply,
                "state_path": str(self.state_path) if self.state_path else None,
                "cty": str(self.cty.path) if self.cty else None,
                "logged_day": self.logged_day,
                "logged_today": sorted(self.logged_today),
                "queue": [
                    {"call": i.call, "note": i.note, "attempts": i.attempts, "added": i.added}
                    for i in self.queue
                ],
                # newest last-heard first
                "recent_cqs": list(reversed(cqs[-80:])),
                "log": list(self.log[-40:]),
            }

    def set_cq_ttl(self, seconds: int) -> str:
        with self.lock:
            self.cq_ttl = max(0, int(seconds))
            self._prune_cqs_locked()
            if self.cq_ttl:
                self.event(f"CQ list TTL {self.cq_ttl}s")
            else:
                self.event("CQ list TTL off (never expire)")
        return f"cq_ttl={self.cq_ttl}"

    def set_timeout_cycles(self, n: int) -> str:
        with self.lock:
            self.timeout_cycles = max(1, int(n))
            self.event(f"TX turns set to {self.timeout_cycles}")
        return f"timeout_cycles={self.timeout_cycles}"

    def set_queued_cq_style(self, style: str) -> str:
        style = (style or "").strip().lower()
        if style not in QUEUED_CQ_STYLES:
            style = "both"
        with self.lock:
            self.queued_cq_style = style
            self.event(f"queued CQ style: {style}")
        return f"queued_cq_style={style}"

    def _roll_logged_day_locked(self) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self.logged_day:
            self.logged_day = day
            self.logged_today.clear()

    def _mark_logged_locked(self, call: str) -> None:
        """Record a QSO for the current UTC day and drop them from Recent CQs."""
        if not call:
            return
        self._roll_logged_day_locked()
        self.logged_today.add(call)
        self.recent_cqs = [e for e in self.recent_cqs if e["call"] != call]

    def _note_cq_locked(self, entry: dict[str, Any]) -> None:
        """Upsert one CQ row per callsign (dedupe), keeping last-heard order."""
        call = entry["call"]
        self._roll_logged_day_locked()
        if call in self.logged_today:
            return
        if self.cty:
            entry.update(self.cty.annotate(call))
        self.recent_cqs = [e for e in self.recent_cqs if e["call"] != call]
        self.recent_cqs.append(entry)
        self._prune_cqs_locked()

    def _prune_cqs_locked(self) -> None:
        self._roll_logged_day_locked()
        if self.logged_today:
            self.recent_cqs = [e for e in self.recent_cqs if e["call"] not in self.logged_today]
        if self.cq_ttl > 0:
            cutoff = time.time() - self.cq_ttl
            self.recent_cqs = [e for e in self.recent_cqs if e["t"] >= cutoff]
        if len(self.recent_cqs) > 80:
            self.recent_cqs = self.recent_cqs[-80:]

    def add(self, call: str, note: str = "") -> str:
        call = call.strip().upper()
        if not call:
            return "empty call"
        with self.lock:
            if any(i.call == call for i in self.queue):
                return f"{call} already queued"
            self.queue.append(QueueItem(call=call, note=note))
            self._save_locked()
            self.event(f"queued {call}" + (f" ({note})" if note else ""))
            ident = self.ident
        self._highlight(ident, call, queued=True)
        return f"queued {call}"

    def remove(self, call: str) -> str:
        call = call.strip().upper()
        with self.lock:
            self.queue = [i for i in self.queue if i.call != call]
            if self.active == call:
                self.active = None
                self.cycles_on_active = 0
                self.heard_reply = False
            self._save_locked()
            ident = self.ident
            self.event(f"removed {call}")
        self._highlight(ident, call, queued=False)
        return f"removed {call}"

    def clear(self) -> str:
        with self.lock:
            n = len(self.queue)
            calls = [i.call for i in self.queue]
            self.queue = []
            self.active = None
            self.cycles_on_active = 0
            self.heard_reply = False
            self._save_locked()
            ident = self.ident
            self.event(f"cleared queue ({n})")
        for call in calls:
            self._highlight(ident, call, queued=False)
        return f"cleared {n}"

    def move(self, call: str, direction: str) -> str:
        """Reorder queue. direction is 'up' (toward top) or 'down'."""
        call = call.strip().upper()
        direction = (direction or "").strip().lower()
        if direction not in ("up", "down"):
            return "direction must be up or down"
        with self.lock:
            idx = next((i for i, q in enumerate(self.queue) if q.call == call), -1)
            if idx < 0:
                return f"{call} not in queue"
            if direction == "up":
                if idx == 0:
                    return f"{call} already at top"
                self.queue[idx - 1], self.queue[idx] = self.queue[idx], self.queue[idx - 1]
            else:
                if idx >= len(self.queue) - 1:
                    return f"{call} already at bottom"
                self.queue[idx + 1], self.queue[idx] = self.queue[idx], self.queue[idx + 1]
            self._save_locked()
            self.event(f"moved {call} {direction}")
        return f"moved {call} {direction}"

    def _higher_priority_fresh_locked(self, target: str) -> Optional[str]:
        """If a station above `target` CQ'd recently, return that callsign."""
        now = time.time()
        fresh = {
            e["call"]
            for e in self.recent_cqs
            if now - float(e["t"]) <= PRIORITY_FRESH_S
        }
        for item in self.queue:
            if item.call == target:
                return None
            if item.call in fresh:
                return item.call
        return None

    def arm(self, value: bool) -> str:
        with self.lock:
            self.armed = value
            if not value:
                self.active = None
                self.cycles_on_active = 0
            self.event("ARMED — will answer queued CQs" if value else "disarmed")
        return "armed" if value else "disarmed"

    def skip(self) -> str:
        with self.lock:
            call = self.active
            ident = self.ident
            if not call:
                return "nothing to skip"
            self._drop_active_locked("skipped")
        self._send(pack_halt_tx(ident, auto_tx_only=False))
        return f"skipped {call}"

    def halt(self) -> str:
        with self.lock:
            ident = self.ident
            self.armed = False
            self.active = None
            self.cycles_on_active = 0
            self.event("Halt Tx + disarmed")
        self._send(pack_halt_tx(ident, auto_tx_only=False))
        return "halted"

    def _drop_active_locked(self, reason: str) -> None:
        call = self.active
        if not call:
            return
        self.event(f"{call}: {reason}")
        self.queue = [i for i in self.queue if i.call != call]
        self.active = None
        self.cycles_on_active = 0
        self.heard_reply = False
        self._save_locked()

    def _busy_locked(self) -> bool:
        st = self.status
        if st.get("transmitting"):
            return True
        if st.get("tx_enabled") and st.get("dx_call"):
            # A QSO is already in progress (ours or the operator's).
            return True
        return False

    def on_status(self, ident: str, st: dict[str, Any], peer: tuple[str, int]) -> None:
        halt_ident = None
        halt_call = None
        with self.lock:
            self.ident = ident
            self.peer = peer
            prev = self.status
            self.status = st
            # Count a finished transmit period as one unanswered TX turn.
            if (
                self.active
                and not self.heard_reply
                and prev.get("transmitting")
                and not st.get("transmitting")
            ):
                self.cycles_on_active += 1
                self.event(
                    f"{self.active}: TX turn {self.cycles_on_active}/{self.timeout_cycles}"
                )
                if self.cycles_on_active >= self.timeout_cycles:
                    halt_call = self.active
                    halt_ident = ident
                    self._drop_active_locked(
                        f"timed out after {self.timeout_cycles} TX turns"
                    )
        if halt_ident:
            self._send(pack_halt_tx(halt_ident, auto_tx_only=False))
            if halt_call:
                self._highlight(halt_ident, halt_call, queued=False)

    def on_clear(self) -> None:
        # Band Activity was erased: prior decodes can no longer be Replied to.
        # The next CQ decode will be used instead. Nothing else to do.
        return

    def on_qso_logged(self, qso: dict[str, Any]) -> None:
        call = (qso.get("dx_call") or "").upper()
        with self.lock:
            band = hz_to_band(self.status["dial_freq"]) if self.status.get("dial_freq") else ""
            mode = (self.status.get("mode") or qso.get("mode") or "").upper()
            self.worked.add((call, band, mode))
            self._mark_logged_locked(call)
            self.event(f"logged {call}  {qso.get('report_sent','')}/{qso.get('report_rcvd','')}  {band}")
            if self.active and self.active == call:
                ident = self.ident
                self._drop_active_locked("QSO logged — next in queue on their next CQ")
            else:
                # Even if we weren't the one who started it, drop them from the queue.
                before = len(self.queue)
                self.queue = [i for i in self.queue if i.call != call]
                ident = self.ident
                if len(self.queue) != before:
                    self._save_locked()
                    self.event(f"{call} removed from queue after log")
        if call:
            self._highlight(ident, call, queued=False)

    @staticmethod
    def _callback_from(msg: str, de: str) -> Optional[str]:
        """If msg is directed at us, return the other station's callsign."""
        if not de:
            return None
        m = DIRECTED_RE.match((msg or "").strip())
        if not m:
            return None
        tocall = m.group("tocall").upper()
        fromcall = m.group("fromcall").upper()
        if tocall != de or fromcall == de:
            return None
        if not CALL_RE.match(fromcall):
            return None
        return fromcall

    def _make_snap_locked(self, ident: str, dec: dict[str, Any], msg: str) -> DecodeSnap:
        return DecodeSnap(
            ident=ident,
            time=dec["time"],
            snr=dec["snr"],
            dt=dec["dt"],
            df=dec["df"],
            mode=dec["mode"],
            message=msg,
            low_confidence=dec["low_confidence"],
        )

    def on_decode(self, ident: str, dec: dict[str, Any], peer: tuple[str, int]) -> None:
        if dec.get("off_air") or not dec.get("new"):
            return
        msg = dec.get("message") or ""
        cq = parse_cq(msg)
        snap = None
        highlight_call: Optional[str] = None
        with self.lock:
            self.ident = ident
            self.peer = peer
            st = self.status
            de = (st.get("de_call") or "").upper()
            band = hz_to_band(st["dial_freq"]) if st.get("dial_freq") else ""
            mode_name = (st.get("mode") or MODE_MARKERS.get(dec.get("mode") or "", "")).upper()

            if cq:
                self._note_cq_locked(
                    {
                        "call": cq["call"],
                        "dir": cq["dir"],
                        "grid": cq["grid"],
                        "snr": dec["snr"],
                        "df": dec["df"],
                        "message": msg,
                        "t": time.time(),
                    }
                )

            callback_from = self._callback_from(msg, de)
            if callback_from and self.active == callback_from:
                # Stay with them until QSO Logged — ignore TX-turn timeout.
                self.heard_reply = True

            if not self.armed:
                return

            special = st.get("special_op") or 0

            # --- Callback: station calling us — pick up and finish the QSO ---
            if callback_from and special not in (6, 7):
                if self.active and self.active != callback_from:
                    pass  # already driving another QSO
                elif self._busy_locked() and (st.get("dx_call") or "").upper() not in (
                    "",
                    callback_from,
                ):
                    self.event(
                        f"{callback_from} called us, but a QSO with {st.get('dx_call')} is in progress"
                    )
                else:
                    tx_live = bool(st.get("tx_enabled") or st.get("transmitting"))
                    if self.active != callback_from:
                        self.active = callback_from
                        self.active_started = time.time()
                        self.cycles_on_active = 0
                        self.heard_reply = True
                        self.event(
                            f"CALLBACK ← {msg}  (snr {dec['snr']}, df {dec['df']})"
                        )
                        snap = self._make_snap_locked(ident, dec, msg)
                        highlight_call = callback_from
                    elif not tx_live:
                        self.heard_reply = True
                        self.event(
                            f"CALLBACK re-REPLY ← {msg}  (snr {dec['snr']}, df {dec['df']})"
                        )
                        snap = self._make_snap_locked(ident, dec, msg)
                        highlight_call = callback_from
                    # else Auto Seq already running — leave it alone

            # --- Queued CQ auto-call (skipped if callback already set a Reply) ---
            if snap is None and cq:
                target = cq["call"]
                if any(i.call == target for i in self.queue):
                    self._roll_logged_day_locked()
                    if self.skip_b4 and (
                        target in self.logged_today or (target, band, mode_name) in self.worked
                    ):
                        why = (
                            f"already logged today UTC ({self.logged_day})"
                            if target in self.logged_today
                            else f"already logged this session on {band} {mode_name}"
                        )
                        self.event(f"{target} skipped ({why})")
                        self.queue = [i for i in self.queue if i.call != target]
                        self._save_locked()
                    elif special in (6, 7):  # Fox / Hound
                        self.event(
                            f"not calling {target}: special op mode is {SPECIAL_OP.get(special)}"
                        )
                    elif self.active and self.active != target:
                        pass
                    elif self._busy_locked() and (st.get("dx_call") or "").upper() not in (
                        "",
                        target,
                    ):
                        self.event(
                            f"{target} CQ heard, but a QSO with {st.get('dx_call')} is already in progress"
                        )
                    elif self.active == target:
                        if not self.heard_reply and self.cycles_on_active < self.timeout_cycles:
                            # WSJT-X often drops Enable Tx after one unanswered call.
                            if not (st.get("tx_enabled") or st.get("transmitting")):
                                self.event(
                                    f"re-REPLY → {msg}  (after {self.cycles_on_active}/{self.timeout_cycles} TX)"
                                )
                                snap = self._make_snap_locked(ident, dec, msg)
                                highlight_call = target
                    else:
                        # Top-down: defer if a higher queue entry is also calling CQ.
                        blocker = self._higher_priority_fresh_locked(target)
                        if not blocker:
                            self.active = target
                            self.active_started = time.time()
                            self.cycles_on_active = 0
                            self.heard_reply = False
                            for i in self.queue:
                                if i.call == target:
                                    i.attempts += 1
                                    break
                            self._save_locked()
                            self.event(
                                f"REPLY → {msg}  (snr {dec['snr']}, df {dec['df']}, mode {dec['mode']!r})"
                            )
                            snap = self._make_snap_locked(ident, dec, msg)
                            highlight_call = target

        if snap is None:
            return
        pkt = pack_reply(
            snap.ident,
            snap.time,
            snap.snr,
            snap.dt,
            snap.df,
            snap.mode,
            snap.message,
            snap.low_confidence,
            modifiers=MOD_NONE,
        )
        self._send(pkt)
        if highlight_call:
            self._highlight(ident, highlight_call, queued=True)

    def _highlight(self, ident: str, call: str, queued: bool) -> None:
        if not call or not ident:
            return
        if queued:
            pkt = pack_highlight(ident, call, bg=(255, 200, 0), fg=(0, 0, 0))
        else:
            pkt = pack_highlight(ident, call, bg=None, fg=None)
        self._send(pkt)

    def _send(self, pkt: bytes) -> None:
        with self.lock:
            sock, peer = self.sock, self.peer
        if not sock or not peer:
            return
        try:
            sock.sendto(pkt, peer)
        except OSError as exc:
            self.event(f"send failed: {exc}")


# ---------------------------------------------------------------------------
# UDP listener
# ---------------------------------------------------------------------------

def open_socket(bind: str, port: int, multicast: Optional[str]) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.bind((bind, port))
    if multicast:
        # Join on INADDR_ANY and on loopback. Local WSJT-X multicast on Linux
        # often arrives only via lo; GridTracker docs also say to include it.
        for iface in ("0.0.0.0", "127.0.0.1"):
            try:
                mreq = struct.pack("4s4s", socket.inet_aton(multicast), socket.inet_aton(iface))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError as exc:
                print(f"multicast join {multicast} on {iface} failed: {exc}", file=sys.stderr)
    sock.settimeout(0.5)
    return sock


def udp_loop(q: CallQueue, sock: socket.socket, stop: threading.Event) -> None:
    q.sock = sock
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            if stop.is_set():
                break
            raise
        try:
            _schema, typ, ident, r = parse_header(data)
        except PacketError:
            continue
        try:
            if typ == STATUS:
                q.on_status(ident, parse_status(r), addr)
            elif typ == DECODE:
                q.on_decode(ident, parse_decode(r), addr)
            elif typ == QSO_LOGGED:
                q.on_qso_logged(parse_qso_logged(r))
            elif typ == CLEAR:
                q.on_clear()
            elif typ == CLOSE:
                q.event(f"{ident} closed")
            elif typ == HEARTBEAT:
                q.peer = addr
                q.ident = ident
        except PacketError as exc:
            q.event(f"parse error type {typ}: {exc}")


# ---------------------------------------------------------------------------
# Minimal localhost web UI
# ---------------------------------------------------------------------------

HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><title>FT8 call queue</title>
<style>
 body{font:15px/1.4 system-ui,sans-serif;max-width:960px;margin:24px auto;padding:0 16px 72px;
      background:#111;color:#eee}
 h1{font-size:1.3rem;margin:0 0 8px}
 .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0}
 button,input{font:inherit;padding:6px 10px;border-radius:6px;border:1px solid #555;background:#222;color:#eee}
 button.arm{background:#245c24} button.disarm,button.halt{background:#6b1d1d}
 button.move{padding:4px 8px;min-width:2em}
 table{border-collapse:collapse;width:100%;margin:12px 0}
 th,td{text-align:left;padding:4px 8px;border-bottom:1px solid #333}
 .ok{color:#8d8} .bad{color:#f88} .muted{color:#aaa}
 pre{background:#1a1a1a;padding:8px;overflow:auto;height:200px}
 tr.queued{background:#2a2810}
 tr.queued td{border-bottom-color:#4a4520}
 .tip{position:relative;cursor:help;border-bottom:1px dotted #666}
 .tip:hover::after{
   content:attr(data-tip);position:absolute;left:0;top:calc(100% + 6px);z-index:40;
   min-width:180px;max-width:280px;padding:8px 10px;border-radius:6px;border:1px solid #555;
   background:#1c1c1c;color:#eee;font-size:12px;font-weight:400;line-height:1.35;
   white-space:normal;box-shadow:0 8px 24px #000a;pointer-events:none}
 .help-fab{
   position:fixed;right:18px;bottom:18px;z-index:50;width:44px;height:44px;border-radius:50%;
   border:1px solid #666;background:#2a2a2a;color:#eee;font:700 1.25rem/1 system-ui,sans-serif;
   cursor:pointer;box-shadow:0 6px 20px #0008}
 .help-fab:hover{background:#333;border-color:#888}
 .help-backdrop{
   display:none;position:fixed;inset:0;z-index:60;background:#0008}
 .help-backdrop.open{display:block}
 .help-panel{
   display:none;position:fixed;right:18px;bottom:72px;z-index:70;width:min(520px,calc(100vw - 24px));
   max-height:min(70vh,640px);overflow:auto;background:#181818;color:#eee;
   border:1px solid #555;border-radius:10px;box-shadow:0 12px 40px #000a;padding:0}
 .help-panel.open{display:flex;flex-direction:column}
 .help-head{
   display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid #333;
   position:sticky;top:0;background:#181818}
 .help-head h2{margin:0;font-size:1rem;flex:1}
 .help-body{padding:12px 16px 20px;font-size:13px;line-height:1.45}
 .help-body h1{font-size:1.15rem;margin:0 0 10px}
 .help-body h2{font-size:1.05rem;margin:18px 0 8px;border-bottom:1px solid #333;padding-bottom:4px}
 .help-body h3{font-size:0.95rem;margin:14px 0 6px}
 .help-body p{margin:0 0 10px}
 .help-body ul,.help-body ol{margin:0 0 10px;padding-left:1.3em}
 .help-body li{margin:0 0 4px}
 .help-body code{background:#222;padding:1px 4px;border-radius:3px;font-size:12px}
 .help-body pre{height:auto;max-height:180px;margin:0 0 10px;font-size:12px}
 .help-body a{color:#9cf}
 .help-body table{font-size:12px;margin:8px 0}
 .help-body th,.help-body td{padding:3px 6px}
</style></head>
<body>
<h1 title="Attended FT8 companion for WSJT-X / JTDX. Arm must be on before it will Reply.">FT8 call queue</h1>
<p class="muted">Attended operator aid. Stay at the radio. Armed must be on to transmit.
  <span class="tip" data-tip="Hover any control for a short tip. Click ? (bottom right) for the full README help.">Tips</span>
</p>
<div class="row">
  <strong id="state" class="tip" data-tip="Arm enables auto-Reply. Callback QSOs stay until logged; otherwise TX-turn budget applies.">…</strong>
  <span id="radio" class="muted tip" data-tip="Live status from the last WSJT-X Status UDP packet (band, mode, DX call, TX)."></span>
</div>
<div class="row">
  <button class="arm tip" data-tip="Enable auto-calling: reply to queued CQs and pick up stations that call you back." onclick="post('/api/arm')">Arm</button>
  <button class="disarm tip" data-tip="Stop starting new QSOs. Does not send Halt Tx by itself." onclick="post('/api/disarm')">Disarm</button>
  <button class="halt tip" data-tip="Send Halt Tx to WSJT-X and disarm immediately." onclick="post('/api/halt')">Halt Tx + disarm</button>
  <button class="tip" data-tip="Drop the current target from the queue, clear active, and Halt Tx." onclick="post('/api/skip')">Skip current</button>
  <label class="muted tip" data-tip="Unanswered TX periods before giving up on a queued CQ. Callbacks are exempt and run until logged.">TX turns
    <select id="tx_turns" onchange="post('/api/tx-turns',{turns:+this.value})">
      <option value="1">1</option>
      <option value="3">3</option>
      <option value="5">5</option>
    </select>
  </label>
</div>
<div class="row">
  <input id="call" class="tip" data-tip="Type a callsign and press Enter or Add. Queue is saved to disk across restarts." placeholder="callsign" onkeydown="if(event.key==='Enter')add()">
  <button class="tip" data-tip="Append this callsign to the queue (worked top-down when several are calling)." onclick="add()">Add to queue</button>
  <label class="muted tip" data-tip="Remove a station from Recent CQs after this long without a new CQ from them.">Clear CQ after
    <select id="cq_ttl" onchange="post('/api/cq-ttl',{seconds:+this.value})">
      <option value="0">never</option>
      <option value="120">2 min</option>
      <option value="300">5 min</option>
      <option value="600">10 min</option>
      <option value="1800">30 min</option>
    </select>
  </label>
</div>
<div class="row">
  <h2 class="tip" style="margin:0;flex:1" data-tip="Stations to answer on their next CQ/QRZ. Higher rows win when two are calling. Persisted on disk.">Queue</h2>
  <button class="tip" data-tip="Empty the entire queue and clear the active target. Does not Halt Tx by itself." onclick="if(confirm('Clear the entire queue?'))post('/api/queue/clear')">Clear queue</button>
</div>
<table id="queue"><thead><tr><th></th><th>Call</th><th>Note</th><th>Attempts</th><th></th></tr></thead><tbody></tbody></table>
<div class="row">
  <h2 class="tip" style="margin:0;flex:1" data-tip="Deduped CQ/QRZ decodes. Age since last heard. Logged-today UTC calls are hidden. DXCC from cty.dat.">Recent CQs</h2>
  <label class="muted tip" data-tip="How already-queued callsigns appear in Recent CQs: none, highlight, hide queue button, or both.">Queued stations
    <select id="queued_cq_style" onchange="post('/api/queued-cq-style',{style:this.value})">
      <option value="none">none</option>
      <option value="highlight">highlight row</option>
      <option value="hide_btn">remove queue button</option>
      <option value="both">both</option>
    </select>
  </label>
</div>
<table id="cqs"><thead><tr><th>Call</th><th>Last CQ</th><th>SNR</th><th>Hz</th><th>Message</th><th class="tip" data-tip="Entity from Big CTY (cty.dat), same source WSJT-X uses locally.">DXCC</th><th></th></tr></thead><tbody></tbody></table>
<h2 class="tip" data-tip="Companion event log (queue changes, Reply, callback, timeouts, logs).">Log</h2>
<pre id="log"></pre>

<button type="button" class="help-fab" id="help_fab" title="Open help (README)" aria-label="Help">?</button>
<div class="help-backdrop" id="help_backdrop" hidden></div>
<div class="help-panel" id="help_panel" role="dialog" aria-label="Help" hidden>
  <div class="help-head">
    <h2>Help</h2>
    <span class="muted" style="font-size:12px">from README.md</span>
    <button type="button" id="help_close" title="Close help">Close</button>
  </div>
  <div class="help-body" id="help_body"><p class="muted">Loading…</p></div>
</div>

<script>
async function post(url, body){
  await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
                  body: body ? JSON.stringify(body) : '{}'});
  await refresh();
}
async function add(){
  const c = document.getElementById('call').value.trim();
  if(!c) return;
  await post('/api/queue',{call:c});
  document.getElementById('call').value='';
}
function ageLabel(sec){
  sec = Math.max(0, Math.floor(+sec || 0));
  if(sec < 60) return sec + 's';
  const m = Math.floor(sec / 60), s = sec % 60;
  if(m < 60) return s ? (m + 'm ' + s + 's') : (m + 'm');
  const h = Math.floor(m / 60), rm = m % 60;
  return rm ? (h + 'h ' + rm + 'm') : (h + 'h');
}
async function refresh(){
  const s = await (await fetch('/api/status')).json();
  const st = s.status || {};
  document.getElementById('state').innerHTML =
    s.armed ? '<span class="ok">ARMED</span>' : '<span class="bad">disarmed</span>'
    + (s.active
        ? (s.heard_reply
            ? ' — QSO with <strong>'+s.active+'</strong> (callback — until logged)'
            : ' — calling <strong>'+s.active+'</strong> ('+s.cycles_on_active+'/'+s.timeout_cycles+' TX)')
        : '');
  document.getElementById('radio').textContent =
    [st.band, st.mode, st.de_call, st.tx_enabled ? 'Tx enabled' : '', st.transmitting ? 'TRANSMITTING' : '',
     st.dx_call ? ('DX '+st.dx_call) : '', st.special_op && st.special_op!=='NONE' ? st.special_op : ''].filter(Boolean).join(' · ');
  const turns = document.getElementById('tx_turns');
  if(document.activeElement !== turns){
    const tv = String(s.timeout_cycles ?? 3);
    if([...turns.options].some(o => o.value === tv)) turns.value = tv;
  }
  const ttl = document.getElementById('cq_ttl');
  if(document.activeElement !== ttl){
    const v = String(s.cq_ttl ?? 600);
    if([...ttl.options].some(o => o.value === v)) ttl.value = v;
    else { ttl.value = v; if(ttl.value !== v){ const o=document.createElement('option'); o.value=v; o.textContent=(+v?((+v)/60)+' min': 'never'); ttl.appendChild(o); ttl.value=v; } }
  }
  const qstyle = document.getElementById('queued_cq_style');
  if(document.activeElement !== qstyle){
    const qs = s.queued_cq_style || 'both';
    if([...qstyle.options].some(o => o.value === qs)) qstyle.value = qs;
  }
  const style = s.queued_cq_style || 'both';
  const doHighlight = style === 'highlight' || style === 'both';
  const hideBtn = style === 'hide_btn' || style === 'both';
  document.querySelector('#queue tbody').innerHTML = (s.queue||[]).map((i, idx, arr) =>
    `<tr><td>
       <button class="move tip" data-tip="Move higher in the queue (answered first when several are calling)." data-call="${attr(i.call)}" onclick="move(this.dataset.call,'up')" ${idx===0?'disabled':''}>↑</button>
       <button class="move tip" data-tip="Move lower in the queue." data-call="${attr(i.call)}" onclick="move(this.dataset.call,'down')" ${idx===arr.length-1?'disabled':''}>↓</button>
     </td><td>${esc(i.call)}</td><td>${esc(i.note||'')}</td><td title="How many times auto-Reply has been started for this call">${i.attempts}</td>
     <td><button class="tip" data-tip="Remove this callsign from the queue." data-call="${attr(i.call)}" onclick="rm(this.dataset.call)">remove</button></td></tr>`
  ).join('') || '<tr><td colspan=5 class="muted">empty</td></tr>';
  document.querySelector('#cqs tbody').innerHTML = (s.recent_cqs||[]).map(i => {
    const queued = !!i.queued;
    const cls = (queued && doHighlight) ? ' class="queued"' : '';
    let action = '';
    if(queued && hideBtn){
      action = '<span class="muted tip" data-tip="Already in the queue.">queued</span>';
    } else {
      action = `<button class="tip" data-tip="Add this callsign to the queue." data-call="${attr(i.call)}" onclick="post('/api/queue',{call:this.dataset.call})">queue</button>`;
    }
    return `<tr${cls}><td>${esc(i.call)}</td><td class="muted tip" data-tip="Time since this station's last CQ/QRZ decode.">${ageLabel(i.age_s)}</td><td>${i.snr}</td><td>${i.df}</td><td>${esc(i.message)}</td>
     <td class="muted tip" data-tip="DXCC entity from cty.dat (not sent over UDP).">${esc(dxccLabel(i))}</td>
     <td>${action}</td></tr>`;
  }).join('') || '<tr><td colspan=7 class="muted">none yet</td></tr>';
  document.getElementById('log').textContent = (s.log||[]).join('\\n');
}
function dxccLabel(i){
  if(i.label) return i.label;
  if(i.entity) return i.entity;
  if(i.cq_zone) return 'CQ Zone '+i.cq_zone;
  return '';
}
function esc(s){ return String(s).replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function attr(s){ return esc(s); }
async function rm(call){
  await fetch('/api/queue/'+encodeURIComponent(call),{method:'DELETE'});
  await refresh();
}
async function move(call, dir){
  await post('/api/queue/move',{call:call,dir:dir});
}

const helpFab = document.getElementById('help_fab');
const helpPanel = document.getElementById('help_panel');
const helpBackdrop = document.getElementById('help_backdrop');
const helpBody = document.getElementById('help_body');
const helpClose = document.getElementById('help_close');
let helpLoaded = false;

function setHelpOpen(open){
  helpPanel.classList.toggle('open', open);
  helpBackdrop.classList.toggle('open', open);
  helpPanel.hidden = !open;
  helpBackdrop.hidden = !open;
}
async function openHelp(){
  setHelpOpen(true);
  if(helpLoaded) return;
  try{
    const r = await fetch('/api/help');
    const j = await r.json();
    helpBody.innerHTML = j.html || '<p class="bad">Help unavailable.</p>';
    helpLoaded = true;
  }catch(e){
    helpBody.innerHTML = '<p class="bad">Failed to load README help.</p>';
  }
}
helpFab.addEventListener('click', openHelp);
helpClose.addEventListener('click', () => setHelpOpen(false));
helpBackdrop.addEventListener('click', () => setHelpOpen(false));
document.addEventListener('keydown', e => { if(e.key === 'Escape') setHelpOpen(false); });

setInterval(refresh, 1000);
refresh();
</script>
</body></html>
"""


def _inline_md(text: str) -> str:
    text = html_lib.escape(text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    return text


def readme_to_help_html(md: str) -> str:
    """Minimal Markdown → HTML for the floating help panel."""
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    in_code = False
    in_ul = False
    in_ol = False
    in_table = False
    para: list[str] = []

    def flush_para() -> None:
        nonlocal para
        if para:
            out.append("<p>" + _inline_md(" ".join(para)) + "</p>")
            para = []

    def close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    def close_table() -> None:
        nonlocal in_table
        if in_table:
            out.append("</tbody></table>")
            in_table = False

    for raw in lines:
        line = raw.rstrip()
        if line.startswith("```"):
            flush_para()
            close_lists()
            close_table()
            if in_code:
                out.append("</pre>")
                in_code = False
            else:
                out.append("<pre>")
                in_code = True
            continue
        if in_code:
            out.append(html_lib.escape(raw))
            continue
        if not line.strip():
            flush_para()
            close_lists()
            close_table()
            continue
        if line.startswith("|") and line.endswith("|"):
            flush_para()
            close_lists()
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.fullmatch(r":?-{3,}:?", c or "") for c in cells):
                continue
            if not in_table:
                out.append("<table><thead><tr>")
                out.append("".join(f"<th>{_inline_md(c)}</th>" for c in cells))
                out.append("</tr></thead><tbody>")
                in_table = True
                continue
            out.append("<tr>" + "".join(f"<td>{_inline_md(c)}</td>" for c in cells) + "</tr>")
            continue
        close_table()
        if line.startswith("### "):
            flush_para()
            close_lists()
            out.append(f"<h3>{_inline_md(line[4:])}</h3>")
            continue
        if line.startswith("## "):
            flush_para()
            close_lists()
            out.append(f"<h2>{_inline_md(line[3:])}</h2>")
            continue
        if line.startswith("# "):
            flush_para()
            close_lists()
            out.append(f"<h1>{_inline_md(line[2:])}</h1>")
            continue
        m_ul = re.match(r"^[-*] (.+)$", line)
        if m_ul:
            flush_para()
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_inline_md(m_ul.group(1))}</li>")
            continue
        m_ol = re.match(r"^(\d+)\. (.+)$", line)
        if m_ol:
            flush_para()
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{_inline_md(m_ol.group(2))}</li>")
            continue
        close_lists()
        para.append(line.strip())
    flush_para()
    close_lists()
    close_table()
    if in_code:
        out.append("</pre>")
    return "\n".join(out)


def load_help_html() -> str:
    try:
        md = README_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        return f"<p class='bad'>Could not read README.md: {html_lib.escape(str(exc))}</p>"
    return readme_to_help_html(md)


def make_handler(q: CallQueue):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, obj: Any, code: int = 200) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _text(self, body: bytes, ctype: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._text(HTML.encode(), "text/html; charset=utf-8")
            elif path == "/api/status":
                self._json(q.snapshot())
            elif path == "/api/help":
                self._json({"html": load_help_html()})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                data = {}
            path = urlparse(self.path).path
            if path == "/api/queue":
                self._json({"ok": q.add(str(data.get("call") or ""), str(data.get("note") or ""))})
            elif path == "/api/queue/move":
                self._json({"ok": q.move(str(data.get("call") or ""), str(data.get("dir") or ""))})
            elif path == "/api/queue/clear":
                self._json({"ok": q.clear()})
            elif path == "/api/arm":
                self._json({"ok": q.arm(True)})
            elif path == "/api/disarm":
                self._json({"ok": q.arm(False)})
            elif path == "/api/halt":
                self._json({"ok": q.halt()})
            elif path == "/api/skip":
                self._json({"ok": q.skip()})
            elif path == "/api/cq-ttl":
                try:
                    seconds = int(data.get("seconds"))
                except (TypeError, ValueError):
                    seconds = 0
                self._json({"ok": q.set_cq_ttl(seconds)})
            elif path == "/api/tx-turns":
                try:
                    turns = int(data.get("turns"))
                except (TypeError, ValueError):
                    turns = 3
                if turns not in (1, 3, 5):
                    turns = 3
                self._json({"ok": q.set_timeout_cycles(turns)})
            elif path == "/api/queued-cq-style":
                self._json({"ok": q.set_queued_cq_style(str(data.get("style") or ""))})
            elif path == "/api/from-alert":
                # GridTracker cr-alert.sh can POST wanted CQs here.
                # Default: add as a suggestion only if "enqueue" is true.
                call = str(data.get("call") or "")
                note = str(data.get("note") or "gt-alert")
                if data.get("enqueue"):
                    self._json({"ok": q.add(call, note)})
                else:
                    q.event(f"alert (not auto-queued): {call} {note}")
                    self._json({"ok": "noted", "call": call})
            else:
                self._json({"error": "not found"}, 404)

        def do_DELETE(self) -> None:
            path = urlparse(self.path).path
            prefix = "/api/queue/"
            if path.startswith(prefix):
                self._json({"ok": q.remove(path[len(prefix):])})
            else:
                self._json({"error": "not found"}, 404)

    return Handler


# ---------------------------------------------------------------------------
# Self-test of the on-wire format
# ---------------------------------------------------------------------------

def self_test() -> None:
    # Known type-4 Reply, field order from official NetworkMessage.hpp.
    ident, t, snr, dt, df = "WSJT-X", 12_345, -12, -0.5, 1269
    mode, msg, low, mods = "~", "CQ NP4TX FK68", False, 0
    pkt = pack_reply(ident, t, snr, dt, df, mode, msg, low, mods)
    schema, typ, got_id, r = parse_header(pkt)
    assert schema == SCHEMA and typ == REPLY and got_id == ident
    assert r.u32() == t and r.i32() == snr
    assert abs(r.f64() - dt) < 1e-12
    assert r.u32() == df and r.utf8() == mode and r.utf8() == msg
    assert r.bool() is False and r.u8() == 0
    assert r.remaining() == 0

    # Round-trip a synthetic Decode (type 2).
    w = Writer()
    w.header(DECODE, ident)
    w.bool(True)
    w.u32(t)
    w.i32(snr)
    w.f64(dt)
    w.u32(df)
    w.utf8(mode)
    w.utf8(msg)
    w.bool(False)
    w.bool(False)
    _s, typ, _i, r = parse_header(w.bytes())
    dec = parse_decode(r)
    assert typ == DECODE and dec["message"] == msg and dec["mode"] == "~"

    assert parse_cq("CQ NP4TX FK68")["call"] == "NP4TX"
    assert parse_cq("CQ DX KF0QGN EM29")["dir"] == "DX"
    assert parse_cq("CQ POTA W1ABC FN42")["call"] == "W1ABC"
    assert parse_cq("QRZ W1ABC/P")["call"] == "W1ABC/P"
    assert parse_cq("CQ") is None
    assert parse_cq("W1ABC KF0QGN EM29") is None
    hl = pack_highlight("WSJT-X", "NP4TX")
    _s, typ, _i, r = parse_header(hl)
    assert typ == HIGHLIGHT_CALLSIGN and r.utf8() == "NP4TX" and r.u8() == 1

    q = CallQueue(cq_ttl=60)
    with q.lock:
        q._note_cq_locked({"call": "W1ABC", "snr": -10, "df": 1000, "message": "CQ W1ABC", "t": time.time() - 5})
        q._note_cq_locked({"call": "W1ABC", "snr": -5, "df": 1100, "message": "CQ W1ABC FN42", "t": time.time()})
        q._note_cq_locked({"call": "K2XYZ", "snr": -12, "df": 500, "message": "CQ K2XYZ", "t": time.time() - 120})
        assert len(q.recent_cqs) == 1 and q.recent_cqs[0]["call"] == "W1ABC"
        assert q.recent_cqs[0]["snr"] == -5
        q._mark_logged_locked("W1ABC")
        assert not q.recent_cqs
        q._note_cq_locked({"call": "W1ABC", "snr": -1, "df": 1, "message": "CQ W1ABC", "t": time.time()})
        assert not q.recent_cqs  # still suppressed for UTC day
    snap = q.snapshot()
    assert snap["cq_ttl"] == 60
    assert "W1ABC" in snap["logged_today"]

    q3 = CallQueue(cq_ttl=600)
    assert q3.add("AA1AAA") == "queued AA1AAA"
    assert q3.add("BB2BBB") == "queued BB2BBB"
    assert q3.add("CC3CCC") == "queued CC3CCC"
    assert q3.move("BB2BBB", "up") == "moved BB2BBB up"
    assert [i.call for i in q3.queue] == ["BB2BBB", "AA1AAA", "CC3CCC"]
    assert q3.move("BB2BBB", "up") == "BB2BBB already at top"
    assert q3.move("AA1AAA", "down") == "moved AA1AAA down"
    assert [i.call for i in q3.queue] == ["BB2BBB", "CC3CCC", "AA1AAA"]
    now = time.time()
    with q3.lock:
        q3.recent_cqs = [
            {"call": "BB2BBB", "snr": -5, "df": 1, "message": "CQ BB2BBB", "t": now},
            {"call": "AA1AAA", "snr": -8, "df": 2, "message": "CQ AA1AAA", "t": now},
        ]
        assert q3._higher_priority_fresh_locked("AA1AAA") == "BB2BBB"
        assert q3._higher_priority_fresh_locked("BB2BBB") is None
        assert q3._higher_priority_fresh_locked("CC3CCC") == "BB2BBB"
        q3.recent_cqs[0]["t"] = now - (PRIORITY_FRESH_S + 5)
        assert q3._higher_priority_fresh_locked("AA1AAA") is None

    assert CallQueue._callback_from("KF0QGN W1ABC -12", "KF0QGN") == "W1ABC"
    assert CallQueue._callback_from("KF0QGN W1ABC RRR", "KF0QGN") == "W1ABC"
    assert CallQueue._callback_from("CQ W1ABC FN42", "KF0QGN") is None
    assert CallQueue._callback_from("W1ABC KF0QGN -10", "KF0QGN") is None

    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "queue.json"
        q4 = CallQueue(state_path=state)
        q4.add("N0CALL")
        q4.add("K1ABC", note="dx")
        q4.move("K1ABC", "up")
        assert state.is_file()
        q5 = CallQueue(state_path=state)
        assert q5.load_state() == 2
        assert [i.call for i in q5.queue] == ["K1ABC", "N0CALL"]
        assert q5.queue[0].note == "dx"
        assert q5.clear() == "cleared 2"
        assert q5.queue == []
        q6 = CallQueue(state_path=state)
        assert q6.load_state() == 0

    help_html = load_help_html()
    assert "<h1>" in help_html and "Callback" in help_html
    assert "<strong>" in readme_to_help_html("**Arm** must be on")
    assert "<code>queue.py</code>" in readme_to_help_html("run `queue.py`")

    cty_path = resolve_cty_path()
    if cty_path:
        cty = CtyDB(cty_path)
        ak = cty.lookup("NL5Y")
        assert ak and ak.entity == "Alaska" and ak.cq_zone == "1", ak
        ar = cty.lookup("LU1VDF")
        assert ar and ar.entity == "Argentina" and ar.cq_zone == "13", ar
        us = cty.lookup("KD2UUC")
        assert us and us.entity == "United States" and us.label == "U.S.A.", us
        q2 = CallQueue(cq_ttl=600, cty=cty)
        with q2.lock:
            q2._note_cq_locked(
                {"call": "NL5Y", "snr": -9, "df": 1000, "message": "CQ NL5Y BP51", "t": time.time()}
            )
        assert q2.snapshot()["recent_cqs"][0]["label"] == "Alaska"
    else:
        print("cty.dat not found — skipped CTY assertions")
    print("self-test ok")


def stdin_loop(q: CallQueue, stop: threading.Event) -> None:
    print(
        "commands: add CALL | del CALL | up CALL | down CALL | clear | arm | disarm | halt | skip | tx-turns N | cq-ttl SECONDS | status | quit",
        flush=True,
    )
    while not stop.is_set():
        line = sys.stdin.readline()
        if not line:
            break
        parts = line.split()
        if not parts:
            continue
        cmd = parts[0].lower()
        if cmd in ("add", "queue") and len(parts) > 1:
            print(q.add(parts[1], " ".join(parts[2:])))
        elif cmd in ("del", "rm", "remove") and len(parts) > 1:
            print(q.remove(parts[1]))
        elif cmd == "up" and len(parts) > 1:
            print(q.move(parts[1], "up"))
        elif cmd == "down" and len(parts) > 1:
            print(q.move(parts[1], "down"))
        elif cmd == "clear":
            print(q.clear())
        elif cmd == "arm":
            print(q.arm(True))
        elif cmd == "disarm":
            print(q.arm(False))
        elif cmd == "halt":
            print(q.halt())
        elif cmd == "skip":
            print(q.skip())
        elif cmd in ("cq-ttl", "cqttl") and len(parts) > 1:
            try:
                print(q.set_cq_ttl(int(parts[1])))
            except ValueError:
                print("cq-ttl needs integer seconds (0 = never)")
        elif cmd in ("tx-turns", "txturns", "turns") and len(parts) > 1:
            try:
                print(q.set_timeout_cycles(int(parts[1])))
            except ValueError:
                print("tx-turns needs integer (1, 3, or 5)")
        elif cmd == "status":
            print(json.dumps(q.snapshot(), indent=2, default=str))
        elif cmd in ("quit", "exit"):
            stop.set()
            break
        else:
            print("unknown command")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bind", default="0.0.0.0", help="UDP bind address (default 0.0.0.0)")
    p.add_argument("--port", type=int, default=2237, help="WSJT-X UDP port (default 2237)")
    p.add_argument("--multicast", default="224.0.0.73",
                   help="multicast group, or empty for unicast (default 224.0.0.73)")
    p.add_argument("--http", default="127.0.0.1:8765", help="web UI bind (default 127.0.0.1:8765)")
    p.add_argument(
        "--timeout-cycles",
        type=int,
        default=3,
        help="unanswered TX turns before Halt (default 3; UI offers 1/3/5)",
    )
    p.add_argument(
        "--cq-ttl",
        type=int,
        default=600,
        help="drop a station from Recent CQs after N seconds without a CQ (0=never, default 600)",
    )
    p.add_argument("--call-b4", action="store_true", help="allow calling stations already logged this session")
    p.add_argument(
        "--cty",
        default="",
        help="path to Big CTY cty.dat (default: ~/.local/share/WSJT-X/cty.dat or /usr/share/wsjtx/cty.dat)",
    )
    p.add_argument(
        "--state",
        default=str(DEFAULT_STATE_PATH),
        help=f"queue save file (default {DEFAULT_STATE_PATH})",
    )
    p.add_argument(
        "--no-state",
        action="store_true",
        help="do not load or save the queue across restarts",
    )
    p.add_argument("--self-test", action="store_true", help="verify UDP packing and exit")
    args = p.parse_args()
    if args.self_test:
        self_test()
        return 0

    cty = load_cty(args.cty or None)
    multicast = args.multicast.strip() or None
    state_path = None if args.no_state else Path(args.state).expanduser()
    q = CallQueue(
        timeout_cycles=args.timeout_cycles,
        skip_b4=not args.call_b4,
        cq_ttl=args.cq_ttl,
        cty=cty,
        state_path=state_path,
    )
    q.load_state()
    sock = open_socket(args.bind, args.port, multicast)
    stop = threading.Event()

    host, http_port = args.http.rsplit(":", 1)
    httpd = ThreadingHTTPServer((host, int(http_port)), make_handler(q))
    threads = [
        threading.Thread(target=udp_loop, args=(q, sock, stop), daemon=True),
        threading.Thread(target=httpd.serve_forever, daemon=True),
    ]
    for t in threads:
        t.start()
    q.event(
        f"listening UDP {args.bind}:{args.port}"
        + (f" multicast {multicast}" if multicast else " unicast")
        + f"  UI http://{args.http}  (disarmed)"
    )
    if state_path:
        q.event(f"queue state {state_path}")
    else:
        q.event("queue persistence off (--no-state)")
    if cty:
        q.event(f"cty.dat {cty.path} ({len(cty.prefixes)} prefixes, {len(cty.exact)} exact)")
    else:
        q.event("cty.dat not found — DXCC column disabled")
    try:
        stdin_loop(q, stop)
    except KeyboardInterrupt:
        print()
    stop.set()
    httpd.shutdown()
    sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
