#!/usr/bin/env python3
"""
SC Log Tracker - a real-time viewer for the Star Citizen Game.log.

Follows the Game.log while you play, turns the raw lines into readable events
and shows them in a local browser page:

  * live event feed (travel, ship, missions, trade, combat, law, party, errors)
  * "Now" panel (location, jurisdiction, zone, ship, quantum target, server)
  * session statistics
  * raw log view with search, filter and auto-scroll

Pure Python standard library, Python 3.9+.

Usage:
    python sc_log_tracker.py                       find the Game.log automatically
    python sc_log_tracker.py "D:\\Games\\StarCitizen\\LIVE"
    python sc_log_tracker.py --replay last         replay the newest file in logbackups
    python sc_log_tracker.py --replay old.log --speed 60

Anti-cheat: the tool only reads the text file the game writes itself. No memory
access, no injection, no hooks. The file is opened briefly on every poll and
closed again, so the game can move it to logbackups on its next start.
"""

import argparse
import collections
import json
import os
import queue
import re
import string
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__version__ = "1.0.0"
APP_NAME = "SC Log Tracker"
REPO_URL = "https://github.com/DefaultHahn/sc-log-tracker"
DEFAULT_PORT = 8777
CHANNELS = ("LIVE", "PTU", "EPTU", "TECH-PREVIEW", "HOTFIX")

POLL_SECONDS = 0.25
READ_CHUNK = 4 * 1024 * 1024
HEAD_BYTES = 256
RAW_KEEP = 1500          # raw lines a newly connected browser receives
EVENTS_KEEP = 20000      # events kept in memory per session
SNAPSHOT_EVENTS = 6000   # events sent in the initial snapshot


def app_dir():
    """Folder next to the script, or next to the .exe when frozen by PyInstaller."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def config_path():
    """sc_log_tracker.json next to the app, or in the user profile if that folder is read-only."""
    local = app_dir() / "sc_log_tracker.json"
    if os.access(local.parent, os.W_OK):
        return local
    base = os.environ.get("LOCALAPPDATA") or os.path.join(Path.home(), ".config")
    return Path(base) / "SC Log Tracker" / "sc_log_tracker.json"


# ---------------------------------------------------------------------------
# Making internal names readable
# ---------------------------------------------------------------------------

MANUF = {
    # ships
    "AEGS": "Aegis", "ANVL": "Anvil", "ARGO": "Argo", "BANU": "Banu",
    "CNOU": "C.O.", "CRUS": "Crusader", "DRAK": "Drake", "ESPR": "Esperia",
    "GAMA": "Gatac", "GRIN": "Greycat", "KRIG": "Kruger", "MISC": "MISC",
    "MRAI": "Mirai", "ORIG": "Origin", "RSI": "RSI", "TMBL": "Tumbril",
    "VNCL": "Vanduul", "XIAN": "Aopoa", "XNAA": "Aopoa", "AOPOA": "Aopoa",
    # weapons and equipment
    "KLWE": "Klaus & Werner", "BEHR": "Behring", "GATS": "Gallenson",
    "HRST": "Hurston Dynamics", "AMRS": "Amon & Reese", "APAR": "Apocalypse Arms",
    "KBAR": "Kroneg", "MXOX": "MaxOx", "CRLF": "CureLife", "SASU": "Sakura Sun",
    "GMNI": "Gemini", "KSAR": "Kastak Arms", "LBCO": "Lightning Bolt",
    "SHIN": "Shubin", "THCN": "Thermyte", "NONE": "",
}
# planets and moons as they appear in internal names (Stanton1b, pyro5e ...)
BODIES = {
    "stanton1": "Hurston", "stanton1a": "Arial", "stanton1b": "Aberdeen",
    "stanton1c": "Magda", "stanton1d": "Ita",
    "stanton2": "Crusader", "stanton2a": "Cellin", "stanton2b": "Daymar", "stanton2c": "Yela",
    "stanton3": "ArcCorp", "stanton3a": "Lyria", "stanton3b": "Wala",
    "stanton4": "microTech", "stanton4a": "Calliope", "stanton4b": "Clio", "stanton4c": "Euterpe",
    "pyro1": "Pyro I", "pyro2": "Monox", "pyro3": "Bloom", "pyro4": "Pyro IV",
    "pyro5": "Pyro V", "pyro5a": "Ignis", "pyro5b": "Vatra", "pyro5c": "Adir",
    "pyro5d": "Fairo", "pyro5e": "Fuego", "pyro5f": "Vuur", "pyro6": "Terminus",
}
# rest stops: RR_<planet>_<point>. The Pyro names were verified against real game logs
# (inventory location code followed by the game's own name for the route start).
STATIONS = {
    "hur_leo": "Everus Harbor", "cru_leo": "Seraphim Station",
    "arc_leo": "Baijini Point", "mic_leo": "Port Tressler",
    "p2_l4": "Checkmate", "p3_leo": "Orbituary", "p3_l1": "Starlight Service Station",
}
PLANET_ABBR = {"hur": "HUR", "cru": "CRU", "arc": "ARC", "mic": "MIC"}
PLANET_NAME = {"hur": "Hurston", "cru": "Crusader", "arc": "ArcCorp", "mic": "microTech"}
SYSTEMS = {"stanton": "Stanton", "stan": "Stanton", "pyro": "Pyro", "nyx": "Nyx",
           "castra": "Castra", "magnus": "Magnus", "terra": "Terra"}
KEEP_WORDS = {"microtech": "microTech", "arccorp": "ArcCorp", "grimhex": "Grim HEX", "curelife": "CureLife"}
SHIP_JUNK = ("default", "dummy", "placeholder", "template", "spawndummy",
             "test_", "_test", "prototype_", "_module", "_ai_", "_npc")


def split_camel(text):
    """QVExtractionStation -> 'QV Extraction Station' (known words are kept)."""
    out = []
    for word in text.split():
        keep = KEEP_WORDS.get(word.lower())
        if keep:
            out.append(keep)
        else:
            out.append(re.sub(r"(?<=[a-z])(?<!Mc)(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", word))
    return " ".join(out)


def pretty_class(cls):
    """AEGS_Gladius_1234567 -> 'Aegis Gladius'."""
    if not cls:
        return ""
    c = re.sub(r"(_\d{3,})+$", "", cls.replace("@vehicle_Name", ""))
    parts = [p for p in c.split("_") if p]
    if not parts:
        return cls
    manufacturer = MANUF.get(parts[0].upper())
    if manufacturer is not None:
        parts[0] = manufacturer
    parts = [x.capitalize() if x.islower() else x for x in parts if x]
    return re.sub(r"\s+", " ", split_camel(" ".join(parts))).strip()


def pretty_item(cls):
    """lbco_sniper_energy_01_mag -> 'Lightning Bolt Sniper Energy (magazine)'."""
    if not cls:
        return ""
    out, mag = [], False
    for part in [p for p in re.split(r"[_\s]+", cls) if p]:
        low = part.lower()
        if re.fullmatch(r"\d{1,3}", part) or low in ("carryable", "consumable", "civilian"):
            continue
        if low == "mag":
            mag = True
            continue
        manufacturer = MANUF.get(part.upper())
        if manufacturer is not None:
            if manufacturer:
                out.append(manufacturer)
            continue
        out.append(part.capitalize() if part.islower() else part)
    text = split_camel(" ".join(out)) or cls
    return text + (" (magazine)" if mag else "")


def pretty_shop(name):
    t = re.sub(r"^SCShop_", "", name or "").replace("RStop", "RestStop")
    filler = ("int", "item", "rund", "rundown", "stall", "med", "otlw")
    tokens = [x for x in re.split(r"[_\-\s]+", t)
              if x and not re.fullmatch(r"\d+|[A-Za-z]", x) and x.lower() not in filler]
    return split_camel(" ".join(tokens))


def _body(code):
    return BODIES.get((code or "").lower())


def _station(planet, point):
    planet, point = planet.lower(), point.lower()
    name = STATIONS.get(f"{planet}_{point}")
    if name:
        return name
    if planet.startswith("p") and planet[1:].isdigit():
        abbr, body = f"PYR{planet[1:]}", BODIES.get("pyro" + planet[1:], "Pyro")
    else:
        abbr, body = PLANET_ABBR.get(planet, planet.upper()), PLANET_NAME.get(planet, planet)
    if point == "leo":
        return f"Rest stop {body} (orbit)"
    return f"{abbr}-{point.upper()}"


def pretty_loc(s):
    """Readable location names:
    RR_CRU_LEO -> Seraphim Station, Outpost_OLP_Stanton2b_Attritus -> Attritus (Daymar),
    OOC_Stanton_2c_Yela -> Yela, Stanton4b_RayariHydro_McGarth -> Rayari Hydro McGarth (Clio)."""
    if not s:
        return ""
    t = s.strip()
    t = re.sub(r"_?\{[0-9A-Fa-f-]{36}\}", "", t).replace(".socpak", "")
    t = re.sub(r"(_\d{6,})+$", "", t)
    low = t.lower()

    if _body(low):
        return _body(low)
    m = re.fullmatch(r"rr_(hur|cru|arc|mic|p\d)_(leo|l\d)", low)
    if m:
        return _station(m.group(1), m.group(2))
    m = re.fullmatch(r"rs_ext_(?:pyro(\d)|(hur|cru|arc|mic))[-_]?(leo|l\d)\d?", low)
    if m:
        return _station("p" + m.group(1) if m.group(1) else m.group(2), m.group(3))
    m = re.fullmatch(r"rr_jp_(stanton|pyro|nyx|castra|magnus|terra)(stanton|pyro|nyx|castra|magnus|terra)", low)
    if m:
        return f"{SYSTEMS[m.group(2)]} Gateway"
    m = re.fullmatch(r"rs_ext_(\w+?)-(\w+?)_jp\d*", low)
    if m and m.group(2) in SYSTEMS:
        return f"{SYSTEMS[m.group(2)]} Gateway"

    t = re.sub(r"^(OOC_|ObjectContainer_)", "", t)
    m = re.match(r"(?i)asteroidclusterbase_([a-z]+)_", t)
    if m:
        return f"Asteroid base ({m.group(1).capitalize()})"
    m = re.fullmatch(r"(?i)(stanton|pyro|nyx)_(\d[a-f]?)_(.+)", t)
    if m:                                    # OOC_Stanton_2c_Yela -> Yela
        return split_camel(m.group(3).replace("_", " "))
    m = re.fullmatch(r"(?i)outpost_[a-z]{3}_((?:stanton|pyro)\d[a-f]?)_([A-Za-z]+)(?:_\d+)?", t)
    if m:
        return f"{split_camel(m.group(2))} ({_body(m.group(1)) or m.group(1)})"
    m = re.fullmatch(r"(?i)((?:stanton|pyro)\d[a-f]?)_(.+)", t)
    if m:
        body = _body(m.group(1)) or m.group(1)
        rest = re.sub(r"(?i)^ASD_Monorail_", "", m.group(2))
        rest = re.sub(r"_\d+$", "", rest)
        if re.fullmatch(r"(?i)outpost(_[a-z0-9]+)+", rest) and rest[7:] == rest[7:].lower():
            kind = "Trading post" if "trdpst" in rest else "Outpost"
            return f"{kind} ({body})"
        return f"{split_camel(rest.replace('_', ' '))} ({body})"
    t = re.sub(r"(?i)^nyx_", "", t)
    # planet/moon code somewhere in the name: PrisonMine_Stanton1b -> Prison Mine (Aberdeen)
    m = re.search(r"(?i)_((?:stanton|pyro)\d[a-f]?)\b", t)
    if m and _body(m.group(1)):
        base = t[:m.start()] + t[m.end():]
        return f"{split_camel(base.replace('_', ' ').strip())} ({_body(m.group(1))})"
    t = re.sub(r"[-_]\d{1,3}$", "", t).replace("_", " ")
    return re.sub(r"\s+", " ", split_camel(t)).strip()


def pretty_dest(s):
    """Readable quantum target. Returns (name, is_real_place)."""
    if not s:
        return "", False
    low = s.lower()
    if low.startswith("partymembermarker"):
        return "Party member", False
    if low.startswith("navpoint_dynamic"):
        return "Marked nav point", False
    m = re.fullmatch(r"(?i)LOC_RR_S([1-4])_L(\d)", s)
    if m:
        return f"{['HUR', 'CRU', 'ARC', 'MIC'][int(m.group(1)) - 1]}-L{m.group(2)}", True
    m = re.fullmatch(r"(?i)MISSION_QT_(.+?)(?:_\d{6,})?", s)
    if m:
        return f"Mission target ({split_camel(m.group(1).replace('_', ' '))})", False
    m = re.match(r"(?i)asteroidcluster_([a-z]+)_region([a-z])_(\S+)", s)
    if m:
        return f"Asteroid field {m.group(3)} ({m.group(1).capitalize()})", False
    if re.search(r"(?i)final|^ab_|^orbtl_|^util_|_encounter|_dungeon|^em_shelter|_cluster_", s):
        return "Mission target", False
    m = re.fullmatch(r"(?i)(levski|grimhex|lorville|area18|orison|newbabbage)(_[a-z]+)?(-\d+)?", s)
    if m:
        return {"grimhex": "Grim HEX", "newbabbage": "New Babbage"}.get(
            m.group(1).lower(), m.group(1).capitalize()), True
    return pretty_loc(s), True


def clean_notif(text):
    """HUD text without markup and language-pack decorations."""
    t = re.sub(r"</?EM\d>", "", text or "").replace("\xa0", " ")
    t = re.sub(r"\[[^\]]*\bRep\]\*?", "", t)
    t = re.sub(r"\[BP\]\*?", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    while t.endswith(":"):
        t = t[:-1].rstrip()
    return t


def fmt_num(n):
    try:
        return f"{float(n):,.0f}"
    except (TypeError, ValueError):
        return str(n)


def money(s):
    return fmt_num((s or "0").replace(",", "").replace(".", ""))


def to_dt(ts):
    try:
        return datetime.fromisoformat(ts[:19] + "+00:00")
    except (TypeError, ValueError):
        return None


def local_time(ts):
    dt = to_dt(ts)
    if not dt:
        return (ts or "--:--:--")[11:19] or "--:--:--"
    return dt.astimezone().strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# Log line patterns (Star Citizen 4.x, verified on 4.10)
# ---------------------------------------------------------------------------

TS_RE = re.compile(r"^<(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)>")
NOTIF_START = 'Added notification "'
NOTIF_FULL_RE = re.compile(r'Added notification "(.*?)" \[(\d+)\]')
NOTIF_END_RE = re.compile(r'^(.*?)" \[(\d+)\]')

HANDLE_RE = re.compile(r"Handle\[([^\]]+)\]")
NICK_RE = re.compile(r'nickname="([^"]+)"\s+playerGEID=')
SHARD_RE = re.compile(r"shard\[([^\]]+)\]")
LOCINV_RE = re.compile(r"<RequestLocationInventory> Player\[([^\]]+)\] requested inventory for Location\[([^\]]+)\]")
ROUTE_RE = re.compile(r"Projected Start Location is (.+?) for route to destination (\S+)")
QT_SELECT = "Player Selected Quantum Target"
QT_DEST_RE = re.compile(r"selected point (\S+) as their destination")
QT_FUEL_DEST_RE = re.compile(r"Player Requested Fuel to Quantum Target.*?destination (\S+)")
QT_SHIP_RE = re.compile(r"\| (?:NOT )?AUTH \| ([A-Za-z][A-Za-z0-9_]+?)_\d{6,}\[")
QT_ARRIVE = ("Quantum Drive Arrived - Arrived at Final Destination",
             "Quantum Drive has arrived at final destination")
CTRL_RE = re.compile(r"control token for '([A-Za-z][A-Za-z0-9_]+?)_\d{6,}'")
COLLISION_RE = re.compile(r"Fatal Collision occured for vehicle (\S+).*?Zone:\s*([^,\]]+)")
DEAD_ACTOR_RE = re.compile(r"Actor '([^']+)'")
DEAD_ZONE_RE = re.compile(r"ejected from zone '([^']+)'")
DEAD_TO_RE = re.compile(r"to zone '([^']+)'")
ENDMISSION_TYPE_RE = re.compile(r"CompletionType\[([^\]]+)\]")
ITEMBUY_RE = re.compile(r"client_price\[([\d.]+)\].*?itemName\[([^\]]+)\] quantity\[(\d+)\]")
COMMBUY_RE = re.compile(r"\bprice\[([\d.]+)\]")
SHOPNAME_RE = re.compile(r"shopName\[([^\]]+)\]")
SHOPRESP_RE = re.compile(r"result\[(\w+)\] type\[(\w+)\]")
MEDBED_VEH_RE = re.compile(r"vehicle name: ([^,]+)")
QUIT_RE = re.compile(r"CSystem::Quit invoked with - cause=(\d+), reason=([^,]+)")
POPUP_RE = re.compile(r"Error Popup Opened> errorCode=(\d+)")
CRASH_MARK = "public crash handler taking over"
CRASH_EXC_RE = re.compile(r"Exception (\w+)\((0x[0-9A-Fa-f]+)\)")
DISCO_RE = re.compile(r'cause=(\d+) reason="([^"]*)"')
FULLVER_RE = re.compile(r"(?:File|Product)Version:\s*([\d.]+)")
EXE_CHANNEL_RE = re.compile(r"[\\/](LIVE|PTU|EPTU|TECH-PREVIEW|HOTFIX)[\\/]Bin64", re.I)
PU_JOIN = ("Context Establisher Done", 'gamerules="SC_Default"', 'establisher="Network"')

# legacy combat lines (patch 4.1-4.3 only; combat moved server-side in the 2026 builds)
KILL_RE = re.compile(
    r"<Actor Death> CActor::Kill: '([^']+)' \[\d+\] in zone '([^']*)' "
    r"killed by '([^']+)' \[\d+\] using '([^']*)' \[Class [^\]]*\] "
    r"with damage type '([^']*)'")
VDESTROY_RE = re.compile(
    r"<Vehicle Destruction> CVehicle::OnAdvanceDestroyLevel: Vehicle '([^']+)' \[\d+\] "
    r"in zone '([^']*)'.*?driven by '([^']*)' \[\d+\] "
    r"advanced from destroy level (\d+) to (\d+) caused by '([^']*)'")

BENIGN_NET = {"30010", "30016", "30028"}
NET_MEANING = {
    "30000": "Connection timed out",
    "30013": "Server raised a signal (server crash?)",
    "30015": "Signed in from somewhere else",
    "30024": "Back-end services unresponsive",
    "41045": "Timed out waiting for game rules",
    "41058": "Player query failed",
    "41070": "Timed out waiting for your character",
    "64004": "Server could not resolve your location",
    "64008": "Server could not resolve your location",
    "64010": "Location lookup timed out",
    "67001": "Server database unreachable",
    "70003": "Authentication timed out",
    "70006": "Client integrity violation",
}
CRASH_KIND = {
    "STATUS_CRYENGINE_GPU_CRASH": "Graphics driver crash",
    "STATUS_CRYENGINE_OUT_OF_SYSMEM": "Out of system memory",
    "STATUS_CRYENGINE_WATCH_DOG": "Game hung (watchdog timeout)",
    "EXCEPTION_ACCESS_VIOLATION": "Game bug (access violation)",
    "EXCEPTION_BREAKPOINT": "Internal assertion failed",
    "STATUS_CRYENGINE_FATAL_ERROR": "Fatal engine error",
}
COMPLETION = {"complete": "completed", "completed": "completed", "fail": "failed", "failed": "failed",
              "abandon": "abandoned", "abandoned": "abandoned",
              "withdraw": "withdrawn", "withdrawn": "withdrawn"}


# ---------------------------------------------------------------------------
# Parser: one line in, zero or more events out
# ---------------------------------------------------------------------------

class Parser:
    def __init__(self):
        self.line_no = 0
        self.seq = 0
        self.last_ts = None
        self.notif_ids = set()
        self.pend = None            # multi-line HUD notification being assembled
        self.last = {}              # last values, used for de-duplication
        self.last_disco = None
        self.state = {
            "handle": None, "shard": None, "channel": None, "version": None,
            "first_ts": None, "last_ts": None,
            "location": None, "jurisdiction": None,
            "armistice": None, "monitored": None,
            "ship": None, "ship_owner": None, "pilot_ship": None,
            "qt_target": None,
            "qt_jumps": 0, "qt_selected": 0,
            "contracts_acc": 0, "contracts_done": 0, "contracts_failed": 0,
            "objectives_done": 0, "missions_ended": 0,
            "deaths": 0, "kills": 0, "injuries": 0,
            "purchases": 0, "spend": 0.0, "blueprints": 0, "received": 0,
            "crimes": 0, "fines": 0,
            "notifs": 0, "crashes": 0, "disconnects": 0, "net_errors": 0,
            "lines": 0, "events": 0,
        }

    # -- helpers --------------------------------------------------------------
    def _ev(self, out, ts, cat, title, detail="", level="info", raw=""):
        self.seq += 1
        self.state["events"] += 1
        out.append({"id": self.seq, "ts": ts, "c": cat, "ti": title, "d": detail,
                    "lv": level, "n": self.line_no, "raw": raw[:700]})

    def _changed(self, key, value):
        if self.last.get(key) == value:
            return False
        self.last[key] = value
        return True

    def _flap(self, key, value, ts, window=90):
        """True if the same zone state was reported only moments ago (flapping at a border)."""
        now = to_dt(ts)
        prev = self.last.get("flap_" + key)
        self.last["flap_" + key] = (value, now)
        return bool(prev and prev[0] == value and now and prev[1]
                    and (now - prev[1]).total_seconds() < window)

    def _set_location(self, loc, ts, out, raw):
        if loc and self._changed("loc", loc):
            self.state["location"] = loc
            self._ev(out, ts, "travel", "Location", loc, "info", raw)

    # -- main entry -------------------------------------------------------------
    def feed(self, line):
        """Parse one log line (without line break). Returns a list of event dicts."""
        self.line_no += 1
        S = self.state
        S["lines"] = self.line_no
        m = TS_RE.match(line)
        if m:
            ts = m.group(1)
            self.last_ts = ts
            S["last_ts"] = ts
            if not S["first_ts"]:
                S["first_ts"] = ts
            body = line[m.end():].strip()
        else:
            ts = self.last_ts
            body = line.strip()
        out = []

        # Continue a multi-line HUD notification. Continuation lines carry a
        # timestamp but no [Notice]/[Error] prefix.
        if self.pend is not None:
            p = self.pend
            p["n"] += 1
            if not body.startswith("[") and NOTIF_START not in line:
                em = NOTIF_END_RE.match(body)
                if em:
                    self.pend = None
                    self._notif(p["text"] + " " + em.group(1), em.group(2), p["ts"], p["raw"], out)
                    return out
                p["text"] += " " + body
                if p["n"] >= 10:
                    self.pend = None
                    self._notif(p["text"], None, p["ts"], p["raw"], out)
                return out
            if NOTIF_START in line or p["n"] >= 10:
                self.pend = None
                self._notif(p["text"], None, p["ts"], p["raw"], out)

        if NOTIF_START in line:
            nm = NOTIF_FULL_RE.search(line)
            if nm:
                self._notif(nm.group(1), nm.group(2), ts, line, out)
            else:
                start = line.index(NOTIF_START) + len(NOTIF_START)
                self.pend = {"text": line[start:], "ts": ts, "raw": line, "n": 0}
            return out

        self._other(line, ts, out)
        return out

    # -- HUD notifications ------------------------------------------------------
    def _notif(self, text, nid, ts, raw, out):
        if nid:
            if nid in self.notif_ids:
                return
            self.notif_ids.add(nid)
        S = self.state
        t = clean_notif(text)
        if not t or t.startswith("Downloading:"):
            return
        S["notifs"] += 1

        def ev(cat, title, detail="", level="info"):
            self._ev(out, ts, cat, title, detail, level, raw)

        # --- missions ---
        m = re.match(r"Contract (Accepted|Complete|Failed|Available|Shared):\s*(.*)", t)
        if m:
            kind, name = m.group(1), m.group(2).strip(" :")
            if kind == "Accepted":
                S["contracts_acc"] += 1
                ev("mission", "Contract accepted", name)
            elif kind == "Complete":
                S["contracts_done"] += 1
                ev("mission", "Contract complete", name, "good")
            elif kind == "Failed":
                S["contracts_failed"] += 1
                ev("mission", "Contract failed", name, "bad")
            elif kind == "Available":
                ev("mission", "Contract available", name)
            else:
                ev("mission", "Contract shared", name)
            return
        m = re.match(r"New Objective:\s*(.*)", t)
        if m:
            return ev("mission", "New objective", m.group(1))
        m = re.match(r"Objective Complete:\s*(.*)", t)
        if m:
            S["objectives_done"] += 1
            return ev("mission", "Objective complete", m.group(1), "good")
        m = re.match(r"Objective Withdrawn:\s*(.*)", t)
        if m:
            return ev("mission", "Objective withdrawn", m.group(1), "warn")
        m = re.match(r"You've earned:\s*(\d+)\s+rewards", t, re.I)
        if m:
            return ev("mission", "Rewards earned", f"{m.group(1)}, collect them at your home location", "good")

        # --- locations and zones ---
        m = re.match(r"(?:Entered (.+?) Jurisdiction|Journal Entry Added: Jurisdiction: (.+))$", t)
        if m:
            j = (m.group(1) or m.group(2)).strip()
            if j != S["jurisdiction"]:
                now = to_dt(ts)
                left = self.last.setdefault("jur_left", {})
                if S["jurisdiction"] and now:
                    left[S["jurisdiction"]] = now
                S["jurisdiction"] = j
                back = left.get(j)
                if back and now and (now - back).total_seconds() < 30:
                    return                   # ping-pong at a border, don't report every flip
                lawless = j.lower() == "ungoverned"
                ev("travel", "Jurisdiction", j + (" (lawless)" if lawless else ""), "warn" if lawless else "info")
            return
        m = re.match(r"Journal Entry Added:\s*(.*)", t)
        if m:
            return ev("notice", "Journal entry", m.group(1))
        if t.startswith("Entering Armistice Zone"):
            was = S["armistice"]
            S["armistice"] = True
            if self._flap("arm", True, ts) and was:
                return
            return ev("travel", "Entered armistice zone", "weapons locked", "good")
        if t.startswith(("Leaving Armistice Zone", "Exiting Armistice Zone")):
            if S["armistice"] is False:
                return
            S["armistice"] = False
            self._flap("arm", False, ts)
            return ev("travel", "Left armistice zone", "weapons free", "warn")
        if t.startswith("Entered Monitored Space"):
            was = S["monitored"]
            S["monitored"] = True
            if self._flap("mon", True, ts) and was:
                return
            return ev("travel", "Entered monitored space")
        if t.startswith("Exited Monitored Space"):
            was = S["monitored"]
            S["monitored"] = False
            if self._flap("mon", False, ts) and was is False:
                return
            return ev("travel", "Left monitored space", "", "warn")
        if t.startswith("Monitored Space Down"):
            S["monitored"] = False
            return ev("travel", "Monitoring down", "comm array offline", "warn")
        if t.startswith("Monitored Space Restored"):
            S["monitored"] = True
            return ev("travel", "Monitoring restored")
        if t.startswith("Entering Private Property"):
            return ev("travel", "Entered private property", "", "warn")
        if t.startswith("Leaving Private Property"):
            return ev("travel", "Left private property")
        if t.startswith("Leaving Restricted Area"):
            return ev("travel", "Left restricted area")
        if t.startswith("Restricted Area"):
            return ev("law", "Restricted area", "vehicles will be impounded", "warn")

        # --- ships ---
        m = re.match(r"You have joined channel '(.+?)'", t)
        if m:
            ch = m.group(1).replace("@vehicle_Name", "").strip()
            if " : " in ch:
                ship, owner = [x.strip() for x in ch.split(" : ", 1)]
                S["ship"], S["ship_owner"] = ship, owner
                whose = "" if owner == S["handle"] else f"  ({owner}'s ship)"
                return ev("ship", "Boarded", ship + whose)
            return ev("social", "Joined channel", ch)
        m = re.match(r"You have left the channel '(.+?)'", t)
        if m:
            ch = m.group(1).replace("@vehicle_Name", "").strip()
            if " : " in ch:
                ship = ch.split(" : ", 1)[0].strip()
                if S["ship"] == ship:
                    S["ship"], S["ship_owner"] = None, None
                return ev("ship", "Left ship", ship)
            return ev("social", "Left channel", ch)
        if t.startswith("Hangar Request Completed"):
            return ev("ship", "Hangar ready", "", "good")
        m = re.match(r"Joined hangar queue\. Your place: (\d+)\. Max\. estimated wait: ([\d.]+)", t)
        if m:
            return ev("ship", "Hangar queue", f"position {m.group(1)}, up to {float(m.group(2)):.0f} s")
        if t.startswith("Low Fuel"):
            return ev("ship", "Low fuel", "", "warn")
        m = re.match(r"Vehicle Impounded:\s*(.*)", t)
        if m:
            return ev("law", "Vehicle impounded", m.group(1), "bad")
        m = re.match(r"Quantum Travel Calibration (Started|Complete) By ?(\S*)", t)
        if m:
            done = m.group(1) == "Complete"
            return ev("travel", "Party quantum: calibration " + ("complete" if done else "started"),
                      m.group(2), "good" if done else "info")
        m = re.match(r"(\S+) has started to QT to your current location", t)
        if m:
            return ev("social", "Jumping to you", m.group(1))

        # --- law ---
        if t.startswith("CrimeStat Rating Increased"):
            S["crimes"] += 1
            return ev("law", "CrimeStat increased", "", "bad")
        m = re.match(r"Crime Committed:\s*(.*)", t)
        if m:
            S["crimes"] += 1
            return ev("law", "Crime committed", m.group(1), "bad")
        m = re.match(r"(\S+) committed (.+?) against you", t)
        if m:
            return ev("law", "Crime against you", f"{m.group(1)}: {m.group(2)} (forgive or report)", "warn")
        m = re.match(r"Fined (\d+) UEC", t)
        if m:
            S["fines"] += int(m.group(1))
            return ev("law", "Fined", fmt_num(m.group(1)) + " UEC", "bad")

        # --- trade ---
        m = re.match(r"You sent (.+?):\s*([\d.,]+)\s*aUEC", t)
        if m:
            return ev("economy", "Money sent", f"{money(m.group(2))} aUEC to {m.group(1)}")
        m = re.match(r"(\S+) has sent you:\s*([\d.,]+)\s*aUEC", t)
        if m:
            S["received"] += int(m.group(2).replace(",", "").replace(".", "") or 0)
            return ev("economy", "Money received", f"{money(m.group(2))} aUEC from {m.group(1)}", "good")
        m = re.match(r"(\S+) wants to send you ([\d.,]+) UEC", t)
        if m:
            return ev("economy", "Money offered", f"{m.group(1)} wants to send you {money(m.group(2))} aUEC")
        if t.startswith("Transaction Complete"):
            return ev("economy", "Transaction complete", "", "good")
        m = re.match(r"A Refinery Work Order has been Completed at (.+)", t)
        if m:
            return ev("economy", "Refinery order complete", pretty_loc(m.group(1).split(":")[0]), "good")
        m = re.match(r"Received Blueprint:\s*(.*)", t)
        if m:
            S["blueprints"] += 1
            return ev("economy", "Blueprint received", m.group(1), "good")
        m = re.match(r"Item Bricking Initiated: Your (.+?)\s+is bricking", t)
        if m:
            return ev("notice", "Item bricking", m.group(1), "warn")
        m = re.match(r"Item Bricked: Your (.+?)\s+is now bricked", t)
        if m:
            return ev("notice", "Item bricked", m.group(1), "bad")

        # --- health ---
        m = re.match(r"(Minor|Moderate|Major|Severe) Injury Detected - (.+?) - Tier (\d)", t, re.I)
        if m:
            S["injuries"] += 1
            minor = m.group(1).lower() in ("minor", "moderate")
            return ev("combat", f"{m.group(1).capitalize()} injury",
                      f"{m.group(2).strip().lower()}, tier {m.group(3)} treatment", "warn" if minor else "bad")
        if t.startswith("Incapacitated"):
            return ev("combat", "Incapacitated", t.partition(":")[2].strip(), "bad")
        if t.startswith("Standby, Local Emergency Services"):
            return ev("combat", "Emergency services en route", "", "warn")

        # --- party ---
        m = re.match(r"Party (\S+) (connected|disconnected)\.?$", t)
        if m:
            online = m.group(2) == "connected"
            seen = self.last.setdefault("party_online", {})
            if seen.get(m.group(1)) == online:
                return                       # re-announced on every server transfer
            seen[m.group(1)] = online
            return ev("social", "Party member online" if online else "Party member offline", m.group(1))
        m = re.match(r"(?:New Member Joined|Member Left) (\S+) has (joined|left) (?:the )?channel '(.+?)'", t)
        if m:
            ship = m.group(3).split(" : ")[0].replace("@vehicle_Name", "").strip()
            return ev("social", "Came aboard" if m.group(2) == "joined" else "Went ashore",
                      f"{m.group(1)}  ({ship})")
        if t.startswith("New Member Joined"):
            pm = re.search(r"(\S+) has joined", t)
            return ev("social", "Party member joined", pm.group(1) if pm else "")
        if t.startswith("Member Left"):
            pm = re.search(r"(\S+) has left", t)
            return ev("social", "Party member left", pm.group(1) if pm else "")
        m = re.match(r"(\S+) Party Invite Received", t)
        if m:
            return ev("social", "Party invite", f"from {m.group(1)}")
        m = re.match(r"(?:Initiated by (\S+) Party Launching|Party Launch Initiated by party leader (\S+?)\.?$)", t)
        if m:
            return ev("social", "Party launch", f"started by {m.group(1) or m.group(2)}")
        if t.startswith("Party Launch Notifications sent"):
            return ev("social", "Party launch", "invites sent to the party")
        m = re.match(r"Party Launch Join queue canceled by party leader (\S+?)\.?$", t)
        if m:
            return ev("social", "Party launch cancelled", f"by {m.group(1)}", "warn")
        m = re.match(r"(\S+) Friend Request", t)
        if m:
            return ev("social", "Friend request", f"from {m.group(1)}")
        if t.startswith("Party Launch Accepted"):
            return ev("social", "Party launch accepted", "", "good")
        m = re.match(r"New Party Leader (\S+) is now party leader", t)
        if m:
            return ev("social", "New party leader", m.group(1))
        if re.match(r"You have created party", t):
            return ev("social", "Party created")
        if re.match(r"You have joined party", t):
            return ev("social", "Joined party", "", "good")
        if t.startswith("You have been kicked from the party"):
            return ev("social", "Kicked from party", "", "warn")
        if t.startswith("Party Disbanded"):
            return ev("social", "Party disbanded")
        m = re.match(r"Friend Added (\S+)", t)
        if m:
            return ev("social", "Friend added", m.group(1), "good")
        m = re.match(r"Joining Session Failed\s*(.*)", t)
        if m:
            return ev("error", "Joining session failed", m.group(1), "warn")

        ev("notice", "HUD notice", t)

    # -- every other line ---------------------------------------------------------
    def _other(self, line, ts, out):
        S = self.state

        def ev(cat, title, detail="", level="info"):
            self._ev(out, ts, cat, title, detail, level, line)

        if "Handle[" in line and "login" in line.lower():
            m = HANDLE_RE.search(line)
            if m and m.group(1) != S["handle"]:
                S["handle"] = m.group(1)
                ev("session", "Logged in", m.group(1), "good")
            return
        if 'nickname="' in line:
            m = NICK_RE.search(line)
            if m and m.group(1) != S["handle"]:
                S["handle"] = m.group(1)
                ev("session", "Logged in", m.group(1), "good")
            return
        if "<Join PU>" in line:
            m = SHARD_RE.search(line)
            if m and m.group(1) != S["shard"]:
                S["shard"] = m.group(1)
                ev("session", "Joined server", m.group(1))
            return
        if all(k in line for k in PU_JOIN):
            return ev("session", "Entered the universe", "persistent universe loaded", "good")

        # --- location and quantum travel ---
        if "<RequestLocationInventory>" in line:
            m = LOCINV_RE.search(line)
            if m and (S["handle"] is None or m.group(1) == S["handle"]):
                self._set_location(pretty_loc(m.group(2)), ts, out, line)
            return
        if "Projected Start Location is" in line:
            m = ROUTE_RE.search(line)
            if m:
                self._set_location(m.group(1).strip(), ts, out, line)
            return
        if QT_SELECT in line:
            m = QT_DEST_RE.search(line)
            dest = pretty_dest(m.group(1))[0] if m else ""
            sm = QT_SHIP_RE.search(line)
            if sm and not any(j in sm.group(1).lower() for j in SHIP_JUNK):
                S["pilot_ship"] = pretty_class(sm.group(1))
            S["qt_selected"] += 1
            if dest:
                S["qt_target"] = dest
            if self._changed("qtsel", dest or ts):
                ev("travel", "Quantum target set", dest)
            return
        if "Player Requested Fuel to Quantum Target" in line:
            m = QT_FUEL_DEST_RE.search(line)
            if m and not S["qt_target"]:
                S["qt_target"] = pretty_dest(m.group(1))[0]
            return
        if any(k in line for k in QT_ARRIVE):
            S["qt_jumps"] += 1
            dest = S["qt_target"] or ""
            self.last.pop("qtsel", None)
            if dest:
                detail = dest
            else:                            # someone else picked the target (crew / party)
                sm = QT_SHIP_RE.search(line)
                detail = f"aboard {pretty_class(sm.group(1))}, target set by the pilot" if sm else ""
            ev("travel", "Quantum jump arrived", detail, "good")
            if dest:
                S["location"] = dest
                self.last["loc"] = dest
            S["qt_target"] = None
            return

        # --- ship ---
        if "control token for '" in line:
            m = CTRL_RE.search(line)
            if m and not any(j in m.group(1).lower() for j in SHIP_JUNK):
                ship = pretty_class(m.group(1))
                if "releasing control token" in line:
                    if self._changed("ctrl", ("off", ship)):
                        S["pilot_ship"] = None
                        ev("ship", "Left pilot seat", ship)
                elif self._changed("ctrl", ("on", ship)):
                    S["pilot_ship"] = ship
                    ev("ship", "Took the controls", ship)
            return
        if "Fatal Collision" in line and "PlayerPilot: 1" in line:
            m = COLLISION_RE.search(line)
            detail = f"{pretty_class(m.group(1))} near {pretty_loc(m.group(2))}" if m else ""
            return ev("combat", "Fatal collision", detail, "bad")

        # --- death and health ---
        if "ActorState] Dead" in line:
            am, zm, tm = DEAD_ACTOR_RE.search(line), DEAD_ZONE_RE.search(line), DEAD_TO_RE.search(line)
            actor = am.group(1) if am else None
            ship = pretty_class(zm.group(1)) if zm else ""
            where = pretty_dest(tm.group(1))[0] if tm else ""
            if "destroyed vehicle" in line and ship:
                detail = f"ship destroyed: {ship}" + (f", near {where}" if where else "")
            else:
                detail = f"near {where}" if where else ""
            if actor is None or S["handle"] is None or actor == S["handle"]:
                S["deaths"] += 1
                S["ship"] = S["ship_owner"] = S["pilot_ship"] = S["qt_target"] = None
                self.last.pop("ctrl", None)
                ev("combat", "You died", detail, "bad")
            else:
                ev("combat", "Player died", actor + (f" ({detail})" if detail else ""))
            return
        if "<MED BED HEAL>" in line:
            m = MEDBED_VEH_RE.search(line)
            return ev("combat", "Treated in med bed", pretty_class(m.group(1).strip()) if m else "", "good")

        # --- missions ---
        if "<EndMission" in line:
            tm = ENDMISSION_TYPE_RE.search(line)
            ctype = tm.group(1) if tm else "?"
            S["missions_ended"] += 1
            low = ctype.lower()
            level = "good" if "complete" in low or "success" in low else (
                "bad" if "fail" in low or "abandon" in low else "info")
            return ev("mission", "Mission ended", COMPLETION.get(low, ctype), level)

        # --- trade ---
        if "BuyRequest" in line and "Sending" in line:
            S["purchases"] += 1
            sm = SHOPNAME_RE.search(line)
            shop = pretty_shop(sm.group(1)) if sm else ""
            at = f"  ({shop})" if shop else ""
            if "Commodity" in line:
                cm = COMMBUY_RE.search(line)
                price = float(cm.group(1)) if cm else 0.0
                S["spend"] += price
                return ev("economy", "Bought cargo", f"{fmt_num(price)} aUEC{at}")
            im = ITEMBUY_RE.search(line)
            if im:
                price, item, qty = float(im.group(1)), pretty_item(im.group(2)), int(im.group(3))
                S["spend"] += price
                count = f"{qty}x " if qty > 1 else ""
                return ev("economy", "Purchase", f"{count}{item} for {fmt_num(price)} aUEC{at}")
            return ev("economy", "Purchase", shop)
        if "RmShopFlowResponse" in line:
            m = SHOPRESP_RE.search(line)
            if m and m.group(1) != "Success":
                return ev("economy", "Purchase failed", m.group(1), "bad")
            return
        if "New Insurance Claim Request" in line:
            return ev("ship", "Insurance claim filed")
        if "Claim Complete" in line and "CWallet" in line:
            return ev("ship", "Insurance claim complete", "", "good")

        # --- crashes, connection, quitting ---
        if CRASH_MARK in line:
            S["crashes"] += 1
            return ev("error", "Crash!", "Star Citizen crashed", "bad")
        if "Exception " in line and S["crashes"]:
            m = CRASH_EXC_RE.search(line)
            if m and self._changed("crashexc", (S["crashes"], m.group(1))):
                return ev("error", "Crash cause", CRASH_KIND.get(m.group(1), m.group(1)), "bad")
            return
        if "CSystem::Quit invoked" in line:
            S["qt_target"] = None
            m = QUIT_RE.search(line)
            code, reason = (m.group(1), m.group(2).strip()) if m else ("", "")
            if code == "30024":
                S["net_errors"] += 1
                return ev("error", "Game closed", "back-end services unresponsive", "bad")
            return ev("session", "Game closed", {
                "Quit via console command": "quit from the menu or console",
                "User closed the application": "window closed"}.get(reason, reason))
        if "Error Popup Opened" in line:
            m = POPUP_RE.search(line)
            code = m.group(1) if m else "?"
            meaning = NET_MEANING.get(code, "")
            return ev("error", "In-game error message", f"code {code}" + (f": {meaning}" if meaning else ""), "warn")
        if "cause=" in line and 'reason="' in line:
            m = DISCO_RE.search(line)
            if not m:
                return
            code, reason = m.group(1), m.group(2)
            in_pu = 'gamerules="SC_Default"' in line
            if code == "30010" or (not in_pu and code in BENIGN_NET):
                return
            now = to_dt(ts)
            if self.last_disco and now and (now - self.last_disco).total_seconds() < 30:
                return
            self.last_disco = now
            S["disconnects"] += 1
            S["qt_target"] = None
            if code == "30016" and "ExitToMenu" in reason:
                return ev("session", "Exited to menu", "by you")
            if code == "30016":
                return ev("session", "Disconnected from server", "back to the menu (exit or kick)", "warn")
            if code == "30028":
                return ev("session", "Disconnected for inactivity", "", "warn")
            S["net_errors"] += 1
            return ev("error", f"Connection error {code}", NET_MEANING.get(code, reason), "bad")

        # --- legacy combat lines (patch 4.1-4.3) ---
        if "CActor::Kill" in line:
            m = KILL_RE.search(line)
            if m:
                victim, _zone, killer, weapon, dtype = m.groups()
                me = S["handle"]
                wpn = pretty_class(re.sub(r"_\d+$", "", weapon))
                if me and killer == me and victim != me:
                    S["kills"] += 1
                    ev("combat", "Kill", f"{victim} with {wpn} ({dtype})", "good")
                elif me and victim == me:
                    S["deaths"] += 1
                    ev("combat", "Killed", f"by {killer} with {wpn} ({dtype})", "bad")
                else:
                    ev("combat", "Kill", f"{killer} -> {victim} ({dtype})")
            return
        if "<Vehicle Destruction>" in line:
            m = VDESTROY_RE.search(line)
            if m:
                veh, _zone, driver, _from, lvl_to, cause = m.groups()
                what = "destroyed" if lvl_to == "2" else "disabled"
                ev("combat", f"Vehicle {what}", f"{pretty_class(veh)} (pilot: {driver}, by {cause})",
                   "bad" if driver == S["handle"] else "info")
            return

        if S["version"] is None and ("FileVersion" in line or "ProductVersion" in line):
            m = FULLVER_RE.search(line)
            if m:
                S["version"] = m.group(1)
                return ev("session", "Game version", m.group(1))
        if S["channel"] is None and "Bin64" in line:
            m = EXE_CHANNEL_RE.search(line)
            if m:
                S["channel"] = m.group(1).upper()


# ---------------------------------------------------------------------------
# Hub: holds the state and fans it out to connected browsers
# ---------------------------------------------------------------------------

class Hub:
    def __init__(self, path, mode, quiet=False):
        self.lock = threading.Lock()
        self.parser = Parser()
        self.events = []
        self.raw = collections.deque(maxlen=RAW_KEEP)
        self.clients = set()
        self.quiet = quiet
        self.meta = {"path": str(path), "mode": mode, "waiting": False,
                     "last_read": 0, "history_done": False, "tool": __version__}

    def snapshot(self):
        return {"t": "snapshot", "events": self.events[-SNAPSHOT_EVENTS:],
                "raw": list(self.raw), "state": dict(self.parser.state), "meta": dict(self.meta)}

    def subscribe(self):
        q = queue.Queue(maxsize=3000)
        with self.lock:
            self.clients.add(q)
            return q, self.snapshot()

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def _send(self, q, msg):
        try:
            q.put_nowait(msg)
        except queue.Full:
            q.dead = True                # browser too slow: let it reconnect
            self.clients.discard(q)

    def _broadcast(self, msg):
        for q in list(self.clients):
            self._send(q, msg)

    def set_meta(self, **kw):
        with self.lock:
            if any(self.meta.get(k) != v for k, v in kw.items()):
                self.meta.update(kw)
                self._broadcast({"t": "meta", "meta": dict(self.meta)})

    def reset(self, note):
        with self.lock:
            self.parser = Parser()
            self.events = []
            self.raw.clear()
            self._broadcast({"t": "reset", "note": note, "state": dict(self.parser.state)})
        if not self.quiet:
            con(f"\n=== {note} ===\n", "96")

    def feed(self, lines, history=False):
        new_events, new_raw = [], []
        with self.lock:
            for ln in lines:
                evs = self.parser.feed(ln)
                r = {"n": self.parser.line_no, "s": ln[:1500]}
                if evs:
                    r["k"] = evs[0]["c"]
                head = ln[:70]
                if "[Error]" in head:
                    r["l"] = "e"
                elif "[Warning]" in head:
                    r["l"] = "w"
                self.raw.append(r)
                new_raw.append(r)
                if evs:
                    self.events.extend(evs)
                    new_events.extend(evs)
            if len(self.events) > EVENTS_KEEP:
                del self.events[:len(self.events) - EVENTS_KEEP]
            self.meta["last_read"] = int(time.time() * 1000)
            if not history:
                self._broadcast({"t": "batch", "ev": new_events, "raw": new_raw,
                                 "state": dict(self.parser.state), "lr": self.meta["last_read"]})
        if not history and not self.quiet:
            for e in new_events:
                print_event(e)

    def history_done(self):
        with self.lock:
            self.meta["history_done"] = True
            snap = self.snapshot()
            for q in list(self.clients):
                self._send(q, snap)
            st = self.parser.state
        if not self.quiet and self.meta["mode"] == "live":
            con(f"Read the current session so far: {fmt_num(st['lines'])} lines, "
                f"{fmt_num(st['events'])} events.", "90")
            con("Now following live ...\n", "92")


# ---------------------------------------------------------------------------
# Sources: live tailer and replay
# ---------------------------------------------------------------------------

class FileTailer(threading.Thread):
    daemon = True

    def __init__(self, path, hub):
        super().__init__(name="tailer")
        self.path = Path(path)
        self.hub = hub

    def run(self):
        pos, buf, head = 0, b"", b""
        catching_up = True
        while True:
            try:
                size = os.path.getsize(self.path)
            except OSError:
                self.hub.set_meta(waiting=True)
                if catching_up:
                    catching_up = False
                    self.hub.history_done()
                time.sleep(1.0)
                continue
            self.hub.set_meta(waiting=False)
            data = b""
            try:
                # open and close on every poll (see module docstring)
                with open(self.path, "rb") as f:
                    h = f.read(HEAD_BYTES)
                    n = min(len(h), len(head))
                    if pos and (size < pos or h[:n] != head[:n]):
                        pos, buf, head = 0, b"", b""
                        catching_up = False
                        self.hub.reset("New game session detected (Game.log was recreated)")
                    if len(h) > len(head):
                        head = h
                    if size > pos:
                        f.seek(pos)
                        data = f.read(min(size - pos, READ_CHUNK))
            except OSError:
                time.sleep(POLL_SECONDS)
                continue

            if data:
                pos += len(data)
                data = buf + data
                parts = data.split(b"\n")
                buf = parts.pop()
                lines = [p.decode("utf-8", "replace").rstrip("\r") for p in parts]
                if lines:
                    self.hub.feed(lines, history=catching_up)
            if catching_up and pos >= size:
                catching_up = False
                self.hub.history_done()
            if not data or not catching_up:
                time.sleep(POLL_SECONDS)


class ReplaySource(threading.Thread):
    daemon = True

    def __init__(self, path, hub, speed):
        super().__init__(name="replay")
        self.path = Path(path)
        self.hub = hub
        self.speed = max(speed, 0.1)

    def run(self):
        self.hub.history_done()
        time.sleep(1.5)                      # give the browser time to connect
        last_dt, batch = None, []
        with open(self.path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.rstrip("\r\n")
                m = TS_RE.match(line)
                if m:
                    dt = to_dt(m.group(1))
                    if dt and last_dt:
                        delay = min((dt - last_dt).total_seconds() / self.speed, 1.5)
                        if delay > 0.03:
                            if batch:
                                self.hub.feed(batch)
                                batch = []
                            time.sleep(delay)
                    if dt:
                        last_dt = dt
                batch.append(line)
                if len(batch) >= 300:
                    self.hub.feed(batch)
                    batch = []
        if batch:
            self.hub.feed(batch)
        self.hub.set_meta(replay_done=True)
        con("Replay finished. The page stays open, press Ctrl+C to quit.", "92")


# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    hub = None
    server_version = "SCLogTracker/" + __version__

    def log_message(self, *args):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            page = PAGE.replace("{{VERSION}}", __version__).replace("{{REPO}}", REPO_URL)
            return self._send(200, "text/html; charset=utf-8", page.encode("utf-8"))
        if path == "/api/state":
            with self.hub.lock:
                body = json.dumps({"state": self.hub.parser.state, "meta": self.hub.meta},
                                  ensure_ascii=False).encode("utf-8")
            return self._send(200, "application/json; charset=utf-8", body)
        if path == "/stream":
            return self._stream()
        self._send(404, "text/plain", b"not found")

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q, snap = self.hub.subscribe()
        try:
            self._sse(snap)
            while not getattr(q, "dead", False):
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._sse(msg)
        except OSError:
            pass
        finally:
            self.hub.unsubscribe(q)

    def _sse(self, msg):
        self.wfile.write(b"data: " + json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n\n")
        self.wfile.flush()


def start_server(hub, port):
    Handler.hub = hub
    last_err = None
    for p in range(port, port + 15):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            srv.daemon_threads = True
            return srv, p
        except OSError as e:
            last_err = e
    raise SystemExit(f"No free port found from {port} upwards: {last_err}")


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------

CAT_LABEL = {"session": "Session", "travel": "Travel", "ship": "Ship", "mission": "Mission",
             "economy": "Trade", "combat": "Combat", "law": "Law", "social": "Party",
             "notice": "Notice", "error": "Error"}
CAT_ANSI = {"session": "90", "travel": "96", "ship": "94", "mission": "93", "economy": "92",
            "combat": "91", "law": "33", "social": "95", "notice": "37", "error": "31;1"}
USE_COLOR = True


def con(text, color=None):
    if USE_COLOR and color:
        text = f"\x1b[{color}m{text}\x1b[0m"
    try:
        print(text, flush=True)
    except Exception:  # noqa: BLE001 - a broken console must never stop the tracker
        pass


def print_event(e):
    label = CAT_LABEL.get(e["c"], e["c"]).ljust(8)
    detail = f" - {e['d']}" if e.get("d") else ""
    con(f"[{local_time(e['ts'])}] {label} {e['ti']}{detail}", CAT_ANSI.get(e["c"]))


# ---------------------------------------------------------------------------
# Finding the Game.log
# ---------------------------------------------------------------------------

def logs_in_folder(folder):
    folder = Path(folder)
    found = []
    if (folder / "Game.log").is_file():
        found.append(folder / "Game.log")
    for ch in CHANNELS:
        p = folder / ch / "Game.log"
        if p.is_file():
            found.append(p)
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def find_game_logs():
    bases = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        for lp in (Path(appdata) / "rsilauncher" / "logs" / "log.log",
                   Path(appdata) / "rsilauncher" / "log.log"):
            try:
                txt = lp.read_text(encoding="utf-8", errors="replace")[-3_000_000:]
            except OSError:
                continue
            for m in re.finditer(r'([A-Za-z]:(?:\\\\|\\|/)[^"\'<>|\r\n]*?StarCitizen)', txt):
                bases.append(Path(m.group(1).replace("\\\\", "\\")))
    if os.name == "nt":
        subs = ("Program Files/Roberts Space Industries/StarCitizen",
                "Program Files (x86)/Roberts Space Industries/StarCitizen",
                "Roberts Space Industries/StarCitizen",
                "Games/Roberts Space Industries/StarCitizen",
                "Games/StarCitizen", "StarCitizen", "SteamLibrary/StarCitizen")
        for d in string.ascii_uppercase[2:]:          # skip A: and B:
            root = Path(f"{d}:/")
            if root.exists():
                bases.extend(root / s for s in subs)
    found, seen = [], set()
    for b in bases:
        for p in [b / "Game.log"] + [b / ch / "Game.log" for ch in CHANNELS]:
            key = str(p).lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                if p.is_file():
                    found.append((p.stat().st_mtime, p))
            except OSError:
                pass
    found.sort(reverse=True)
    return [p for _, p in found]


def load_config():
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


def resolve_log(arg):
    if arg:
        p = Path(arg.strip().strip('"'))
        if p.is_dir():
            logs = logs_in_folder(p)
            if logs:
                return logs[0]
            return (p / "LIVE" / "Game.log") if (p / "LIVE").is_dir() else (p / "Game.log")
        return p
    cfg = load_config()
    if cfg.get("log") and Path(cfg["log"]).parent.is_dir():
        return Path(cfg["log"])
    logs = find_game_logs()
    if logs:
        return logs[0]
    con("Could not find your Game.log automatically.", "93")
    con("Paste the path to your StarCitizen folder, LIVE folder or Game.log")
    con(r"(e.g. C:\Program Files\Roberts Space Industries\StarCitizen\LIVE):", "90")
    try:
        answer = input("> ").strip()
    except EOFError:
        answer = ""
    if not answer:
        raise SystemExit("No path given.")
    return resolve_log(answer)


def resolve_replay(arg, live_path):
    if arg.lower() in ("last", "latest"):
        folder = Path(live_path).parent / "logbackups"
        try:
            files = sorted(folder.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            files = []
        if not files:
            raise SystemExit(f"No old logs found in {folder}")
        return files[0]
    p = Path(arg.strip().strip('"'))
    if not p.is_file():
        raise SystemExit(f"File not found: {p}")
    return p


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="sc_log_tracker",
        description="Real-time viewer for the Star Citizen Game.log.",
        epilog=REPO_URL)
    ap.add_argument("log", nargs="?", help="Game.log, LIVE folder or StarCitizen folder")
    ap.add_argument("--replay", metavar="FILE",
                    help="replay an old log file ('last' = newest file in logbackups)")
    ap.add_argument("--speed", type=float, default=30.0, help="replay speed (default 30x)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"web port (default {DEFAULT_PORT})")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    ap.add_argument("--quiet", action="store_true", help="don't print events to the console")
    ap.add_argument("--no-color", action="store_true", help="plain console output")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return ap.parse_args(argv)


def run(args):
    global USE_COLOR
    if os.name == "nt":
        os.system("")                    # enable ANSI colours in the Windows console
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass
    USE_COLOR = not args.no_color

    con(f"{APP_NAME} {__version__}", "96")

    replay_last = bool(args.replay) and args.replay.lower() in ("last", "latest")
    live_path = None if args.replay and not replay_last else resolve_log(args.log)

    if args.replay:
        src_path = resolve_replay(args.replay, live_path or ".")
        hub = Hub(src_path, "replay", args.quiet)
        source = ReplaySource(src_path, hub, args.speed)
        con(f"Replay:   {src_path}  ({args.speed:g}x speed)")
    else:
        save_config({"log": str(live_path)})
        hub = Hub(live_path, "live", args.quiet)
        source = FileTailer(live_path, hub)
        con(f"Game.log: {live_path}")
        if not live_path.exists():
            con("The file doesn't exist yet. Waiting for Star Citizen to start ...", "93")

    srv, port = start_server(hub, args.port)
    url = f"http://127.0.0.1:{port}/"
    con(f"Viewer:   {url}", "92")
    con("Press Ctrl+C to quit.\n", "90")
    threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()
    source.start()
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        con("\nStopped.", "90")
        srv.shutdown()


def main(argv=None):
    args = parse_args(argv)
    try:
        run(args)
    except SystemExit as e:
        # keep the window open when started by double-click (.exe), so the message can be read
        if e.code not in (None, 0) and getattr(sys, "frozen", False):
            print(e.code if isinstance(e.code, str) else "")
            try:
                input("Press Enter to close ...")
            except EOFError:
                pass
            sys.exit(1)
        raise


# ---------------------------------------------------------------------------
# Web page (single page, works offline)
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SC Log Tracker</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%233cc8f2' stroke-width='2'%3E%3Ccircle cx='12' cy='12' r='9'/%3E%3Cpath d='M12 3v4M12 17v4M3 12h4M17 12h4'/%3E%3Ccircle cx='12' cy='12' r='2' fill='%233cc8f2'/%3E%3C/svg%3E">
<style>
:root{
  --bg:#060a11;--panel:#0b121d;--panel2:#0f1826;--line:#18233a;--line2:#111a2b;
  --text:#d6e2f3;--muted:#7488a6;--faint:#3d4c66;--accent:#3cc8f2;
  --good:#6fe0a0;--bad:#ff8080;--warn:#ffc266;
  --c-session:#8d9bb5;--c-travel:#3cc8f2;--c-ship:#7f9dff;--c-mission:#f2c94c;--c-economy:#4fd18b;
  --c-combat:#ff6464;--c-law:#ff9a3d;--c-social:#c792ea;--c-notice:#9aa8bd;--c-error:#ff3d63;
  --mono:"Cascadia Mono","Consolas","SFMono-Regular",monospace;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--text);
  font:14px/1.45 "Segoe UI","Inter",system-ui,sans-serif}
body{display:flex;flex-direction:column;overflow:hidden}
button,input{font:inherit;color:inherit}
a{color:inherit}
header{display:flex;align-items:center;gap:16px;padding:10px 16px;border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,#0a1322,#060a11)}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;letter-spacing:.14em;font-size:12px;
  text-transform:uppercase;color:var(--accent);white-space:nowrap;text-decoration:none}
.brand svg{width:20px;height:20px}
.status{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--muted);white-space:nowrap}
.dot{width:9px;height:9px;border-radius:50%;background:var(--faint)}
.dot.live{background:var(--good);animation:pulse 2s infinite}
.dot.idle{background:var(--warn)}
.dot.off{background:var(--c-error)}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(111,224,160,.55)}70%{box-shadow:0 0 0 8px rgba(111,224,160,0)}100%{box-shadow:0 0 0 0 rgba(111,224,160,0)}}
.who{display:flex;gap:14px;font-size:13px;min-width:0;overflow:hidden}
.who span{white-space:nowrap}
.who b{color:var(--text);font-weight:600}
.who .k{color:var(--muted)}
.path{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--faint);overflow:hidden;white-space:nowrap;max-width:34vw}
.btn{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:5px 11px;cursor:pointer;
  font-size:12px;white-space:nowrap}
.btn:hover{border-color:var(--accent)}
.btn.on{border-color:var(--accent);color:var(--accent)}
main{flex:1;display:grid;grid-template-columns:310px 1fr;min-height:0}
aside{border-right:1px solid var(--line);overflow:auto;padding:14px;display:flex;flex-direction:column;gap:16px}
h2{margin:0 0 8px;font-size:11px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.now{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:4px 12px}
.now .r{display:flex;justify-content:space-between;align-items:baseline;gap:10px;padding:7px 0;border-bottom:1px solid var(--line2)}
.now .r:last-child{border-bottom:0}
.now .k{font-size:12px;color:var(--muted);white-space:nowrap}
.now .v{text-align:right;font-weight:600;overflow-wrap:anywhere}
.now .v.empty{color:var(--faint);font-weight:400}
.pill{display:inline-block;font-size:11px;font-weight:600;padding:1px 8px;border-radius:999px;border:1px solid}
.pill.safe{color:var(--good);border-color:rgba(111,224,160,.4)}
.pill.risk{color:var(--warn);border-color:rgba(255,194,102,.4)}
.pill.danger{color:var(--bad);border-color:rgba(255,128,128,.4)}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:9px 11px;min-width:0}
.tile .v{font-size:21px;font-weight:650;font-variant-numeric:tabular-nums;line-height:1.15}
.tile .k{font-size:11px;color:var(--muted);margin-top:2px}
.tile .s{font-size:11px;color:var(--faint);margin-top:2px}
.tile.wide{grid-column:1/-1}
.note{font-size:11.5px;color:var(--faint);line-height:1.5;margin:0}
.foot{font-size:11px;color:var(--faint);margin-top:auto}
section.feed{display:flex;flex-direction:column;min-height:0;min-width:0}
.tabs{display:flex;gap:2px;padding:0 12px;border-bottom:1px solid var(--line)}
.tab{background:none;border:0;border-bottom:2px solid transparent;padding:10px 12px;cursor:pointer;color:var(--muted);font-weight:600}
.tab.on{color:var(--text);border-bottom-color:var(--accent)}
.tab .n{font-weight:400;color:var(--faint);margin-left:4px;font-variant-numeric:tabular-nums}
.toolbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:9px 12px;border-bottom:1px solid var(--line)}
.chip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--panel);
  color:var(--faint);border-radius:999px;padding:3px 10px;font-size:12px;cursor:pointer;user-select:none}
.chip .sw{width:8px;height:8px;border-radius:50%;background:var(--cc);opacity:.35}
.chip.on{color:var(--text);border-color:color-mix(in srgb,var(--cc) 55%,var(--line))}
.chip.on .sw{opacity:1}
.chip .cnt{color:var(--muted);font-variant-numeric:tabular-nums}
.search{margin-left:auto;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:5px 10px;width:220px;outline:0}
.search:focus{border-color:var(--accent)}
.list{flex:1;overflow:auto;min-height:0}
.ev{display:grid;grid-template-columns:70px 86px minmax(0,1fr);gap:12px;padding:7px 14px;border-bottom:1px solid var(--line2);cursor:pointer}
.ev:hover{background:#0a1220}
.ev .t{font-family:var(--mono);font-size:12px;color:var(--muted);padding-top:1px}
.ev .b{font-size:10.5px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--cc);
  border-left:3px solid var(--cc);padding-left:7px;align-self:start;margin-top:2px;white-space:nowrap}
.ev .ti{font-weight:600}
.ev .d{color:var(--muted);margin-left:8px;overflow-wrap:anywhere}
.ev.good .ti{color:var(--good)} .ev.bad .ti{color:var(--bad)} .ev.warn .ti{color:var(--warn)}
.ev .raw{grid-column:2/-1;display:none;font-family:var(--mono);font-size:11.5px;color:#8fa1bb;white-space:pre-wrap;
  word-break:break-all;background:#080e18;border:1px solid var(--line2);padding:7px 9px;border-radius:5px}
.ev.open .raw{display:block}
.ev.fresh{animation:fresh 3s ease-out}
@keyframes fresh{from{background:rgba(60,200,242,.16)}to{background:transparent}}
.rawlist{font-family:var(--mono);font-size:12px;line-height:1.5;padding:4px 0}
.rl{display:grid;grid-template-columns:64px minmax(0,1fr);padding:0 14px 0 11px;border-left:3px solid transparent;color:#8193ad}
.rl .tx{white-space:pre-wrap;word-break:break-all}
.rl .ln{color:var(--faint);user-select:none}
.rl.hit{border-left-color:var(--cc);color:var(--text);background:#0a1220}
.rl.e{color:#ff8fa3} .rl.w{color:#e8c47e}
mark{background:rgba(60,200,242,.28);color:inherit;border-radius:2px}
.empty-msg{padding:40px 20px;text-align:center;color:var(--faint)}
.toast{position:fixed;right:18px;bottom:18px;background:var(--panel2);border:1px solid var(--accent);border-radius:8px;
  padding:10px 14px;font-size:13px;opacity:0;transform:translateY(8px);transition:.25s;pointer-events:none}
.toast.show{opacity:1;transform:none}
@media (max-width:860px){
  main{grid-template-columns:1fr;grid-template-rows:auto 1fr}
  aside{border-right:0;border-bottom:1px solid var(--line);max-height:42vh}
  .path,.who .opt{display:none}
  .search{width:100%;margin-left:0}
  .ev{grid-template-columns:62px minmax(0,1fr)} .ev .b{display:none} .ev .raw{grid-column:1/-1}
}
</style>
</head>
<body>
<header>
  <a class="brand" href="{{REPO}}" target="_blank" rel="noopener" title="SC Log Tracker {{VERSION}} on GitHub">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9"/><path d="M12 3v4M12 17v4M3 12h4M17 12h4"/><circle cx="12" cy="12" r="2.2" fill="currentColor"/></svg>
    SC Log Tracker
  </a>
  <div class="status"><span id="dot" class="dot"></span><span id="statusText">Connecting ...</span></div>
  <div class="who">
    <span><span class="k">Player</span> <b id="hHandle">–</b></span>
    <span class="opt"><span class="k">Channel</span> <b id="hChannel">–</b></span>
    <span class="opt"><span class="k">Version</span> <b id="hVersion">–</b></span>
  </div>
  <div class="path" id="hPath"></div>
  <button class="btn" id="btnExport" title="Save this session's events as JSON">Export</button>
</header>
<main>
  <aside>
    <div>
      <h2>Now</h2>
      <div class="now">
        <div class="r"><span class="k">Location</span><span class="v" id="nLoc"></span></div>
        <div class="r"><span class="k">Jurisdiction</span><span class="v" id="nJur"></span></div>
        <div class="r"><span class="k">Zone</span><span class="v" id="nZone"></span></div>
        <div class="r"><span class="k">Ship</span><span class="v" id="nShip"></span></div>
        <div class="r"><span class="k">Quantum target</span><span class="v" id="nQt"></span></div>
        <div class="r"><span class="k">Server</span><span class="v" id="nShard"></span></div>
      </div>
    </div>
    <div>
      <h2>Session</h2>
      <div class="stats" id="stats"></div>
    </div>
    <p class="note">Since the 2026 builds, kills, K/D and your aUEC balance are no longer written to the Game.log (they moved server-side). Deaths only show up in some cases, for example when your ship is destroyed.</p>
    <p class="foot">SC Log Tracker {{VERSION}} · <a href="{{REPO}}" target="_blank" rel="noopener">GitHub</a></p>
  </aside>
  <section class="feed">
    <div class="tabs">
      <button class="tab on" data-tab="ev">Events<span class="n" id="cEv">0</span></button>
      <button class="tab" data-tab="raw">Raw log<span class="n" id="cRaw">0</span></button>
    </div>
    <div class="toolbar">
      <span id="chips" style="display:contents"></span>
      <button class="btn" id="btnHits" style="display:none" title="Only show lines that produced an event">Matches only</button>
      <button class="btn on" id="btnScroll" style="display:none">Auto-scroll</button>
      <input class="search" id="search" type="search" placeholder="Search ..." autocomplete="off">
    </div>
    <div class="list" id="evList"></div>
    <div class="list rawlist" id="rawList" style="display:none"></div>
  </section>
</main>
<div class="toast" id="toast"></div>
<script>
const CATS={session:"Session",travel:"Travel",ship:"Ship",mission:"Mission",economy:"Trade",
  combat:"Combat",law:"Law",social:"Party",notice:"Notice",error:"Error"};
const MAX_RAW=4000, MAX_EV_DOM=2500;
let events=[], raw=[], state={}, meta={};
let active=new Set(Object.keys(CATS)), query="", tab="ev", onlyHits=false, autoScroll=true;
let connected=false, lastLineAt=0;
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num=n=>Number(n||0).toLocaleString("en-US",{maximumFractionDigits:0});
function tLocal(ts){ if(!ts) return "--:--:--"; const d=new Date(ts); return isNaN(d)?ts.slice(11,19):d.toLocaleTimeString("en-GB",{hour12:false}); }
function hl(s){ s=esc(s); if(!query) return s; const q=esc(query).replace(/[.*+?^${}()|[\]\\]/g,"\\$&"); return s.replace(new RegExp(q,"gi"),m=>"<mark>"+m+"</mark>"); }
function matches(e){ if(!active.has(e.c)) return false; if(!query) return true;
  const q=query.toLowerCase(); return (e.ti+" "+e.d+" "+CATS[e.c]).toLowerCase().includes(q) || (e.raw||"").toLowerCase().includes(q); }

/* ---------- events ---------- */
function evHtml(e,fresh){
  return `<div class="ev ${e.lv}${fresh?" fresh":""}" style="--cc:var(--c-${e.c})" data-id="${e.id}">
    <span class="t">${tLocal(e.ts)}</span><span class="b">${CATS[e.c]||e.c}</span>
    <div><span class="ti">${hl(e.ti)}</span>${e.d?`<span class="d">${hl(e.d)}</span>`:""}</div>
    <div class="raw">Line ${e.n}: ${hl(e.raw)}</div></div>`;
}
function renderEvents(){
  const list=$("evList"); const shown=[];
  for(let i=events.length-1;i>=0 && shown.length<MAX_EV_DOM;i--) if(matches(events[i])) shown.push(events[i]);
  list.innerHTML = shown.length ? shown.map(e=>evHtml(e,false)).join("")
    : `<div class="empty-msg">${events.length?"No events match this filter.":"No events yet. As soon as Star Citizen writes something to the log, it shows up here."}</div>`;
}
function addEvents(evs){
  const list=$("evList"); const ok=evs.filter(matches); if(!ok.length) return;
  const em=list.querySelector(".empty-msg"); if(em) em.remove();
  list.insertAdjacentHTML("afterbegin", ok.reverse().map(e=>evHtml(e,true)).join(""));
  while(list.children.length>MAX_EV_DOM) list.lastElementChild.remove();
}
$("evList").addEventListener("click",ev=>{ const row=ev.target.closest(".ev"); if(row && !window.getSelection().toString()) row.classList.toggle("open"); });

/* ---------- raw log ---------- */
function rawOk(r){ if(onlyHits && !r.k) return false; return !query || r.s.toLowerCase().includes(query.toLowerCase()); }
function rawHtml(r){ return `<div class="rl${r.k?" hit":""}${r.l?" "+r.l:""}"${r.k?` style="--cc:var(--c-${r.k})"`:""}><span class="ln">${r.n}</span><span class="tx">${hl(r.s)}</span></div>`; }
function renderRaw(){
  const list=$("rawList"); const ok=raw.filter(rawOk);
  list.innerHTML = ok.length ? ok.map(rawHtml).join("") : `<div class="empty-msg">No lines.</div>`;
  if(autoScroll) list.scrollTop=list.scrollHeight;
}
function addRaw(rs){
  const list=$("rawList"); const ok=rs.filter(rawOk); if(!ok.length) return;
  const em=list.querySelector(".empty-msg"); if(em) em.remove();
  list.insertAdjacentHTML("beforeend", ok.map(rawHtml).join(""));
  while(list.children.length>MAX_RAW) list.firstElementChild.remove();
  if(autoScroll && tab==="raw") list.scrollTop=list.scrollHeight;
}
$("rawList").addEventListener("scroll",()=>{ const l=$("rawList"); const atEnd=l.scrollHeight-l.scrollTop-l.clientHeight<40;
  if(autoScroll!==atEnd){ autoScroll=atEnd; $("btnScroll").classList.toggle("on",autoScroll);} });

/* ---------- filters and tabs ---------- */
function renderChips(){
  const counts={}; for(const e of events) counts[e.c]=(counts[e.c]||0)+1;
  $("chips").innerHTML = tab!=="ev" ? "" : Object.entries(CATS).map(([k,l])=>
    `<span class="chip${active.has(k)?" on":""}" data-c="${k}" style="--cc:var(--c-${k})" title="Click: show/hide · Double-click: only this"><span class="sw"></span>${l}<span class="cnt">${counts[k]||0}</span></span>`).join("");
}
let chipTimer=null;
$("chips").addEventListener("click",ev=>{ const c=ev.target.closest(".chip"); if(!c) return;
  clearTimeout(chipTimer); chipTimer=setTimeout(()=>{ const k=c.dataset.c; active.has(k)?active.delete(k):active.add(k); renderChips(); renderEvents(); },200); });
$("chips").addEventListener("dblclick",ev=>{ const c=ev.target.closest(".chip"); if(!c) return; clearTimeout(chipTimer);
  const k=c.dataset.c; active = (active.size===1 && active.has(k)) ? new Set(Object.keys(CATS)) : new Set([k]); renderChips(); renderEvents(); });
document.querySelectorAll(".tab").forEach(b=>b.addEventListener("click",()=>{
  tab=b.dataset.tab; document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("on",x===b));
  $("evList").style.display=tab==="ev"?"":"none"; $("rawList").style.display=tab==="raw"?"":"none";
  $("btnHits").style.display=$("btnScroll").style.display=tab==="raw"?"":"none";
  renderChips(); if(tab==="raw"){ renderRaw(); } }));
let searchTimer=null;
$("search").addEventListener("input",e=>{ clearTimeout(searchTimer); searchTimer=setTimeout(()=>{ query=e.target.value.trim(); renderEvents(); if(tab==="raw") renderRaw(); },150); });
$("btnHits").addEventListener("click",()=>{ onlyHits=!onlyHits; $("btnHits").classList.toggle("on",onlyHits); renderRaw(); });
$("btnScroll").addEventListener("click",()=>{ autoScroll=!autoScroll; $("btnScroll").classList.toggle("on",autoScroll); if(autoScroll){ const l=$("rawList"); l.scrollTop=l.scrollHeight; } });

/* ---------- state ---------- */
function setNow(id,val,html){ const el=$(id); if(val){ el.innerHTML=html??esc(val); el.classList.remove("empty"); } else { el.textContent="–"; el.classList.add("empty"); } }
function dur(a,b){ if(!a||!b) return "–"; let s=Math.max(0,(new Date(b)-new Date(a))/1000|0); const h=s/3600|0; s%=3600; const m=s/60|0; s%=60;
  return (h?h+"h ":"")+String(m).padStart(h?2:1,"0")+"m "+String(s).padStart(2,"0")+"s"; }
function renderState(){
  const s=state;
  $("hHandle").textContent=s.handle||"–"; $("hChannel").textContent=s.channel||"–"; $("hVersion").textContent=s.version||"–";
  setNow("nLoc",s.location); setNow("nJur",s.jurisdiction);
  let zone=null, zh=null;
  if(s.armistice===true){ zone=1; zh='<span class="pill safe">Armistice</span>'; }
  else if(s.monitored===true){ zone=1; zh='<span class="pill risk">Monitored</span>'; }
  else if(s.monitored===false){ zone=1; zh='<span class="pill danger">Unmonitored</span>'; }
  else if(s.armistice===false){ zone=1; zh='<span class="pill risk">No armistice</span>'; }
  setNow("nZone",zone,zh);
  const ship=s.ship||s.pilot_ship;
  setNow("nShip",ship, ship? esc(ship)+(s.ship_owner&&s.ship_owner!==s.handle?`<div class="note">owned by ${esc(s.ship_owner)}</div>`:"") : null);
  setNow("nQt",s.qt_target); setNow("nShard",s.shard);
  const tiles=[
    ["Session time",dur(s.first_ts,s.last_ts),`${num(s.lines)} log lines read`,"wide"],
    ["Quantum jumps",num(s.qt_jumps),`targets set: ${num(s.qt_selected)}`],
    ["Contracts done",num(s.contracts_done),`${num(s.contracts_acc)} accepted · ${num(s.contracts_failed)} failed`],
    ["Objectives done",num(s.objectives_done),`missions ended: ${num(s.missions_ended)}`],
    ["Deaths",num(s.deaths),`injuries: ${num(s.injuries)}`],
    ["Purchases",num(s.purchases),`${num(s.spend)} aUEC spent`+(s.received?` · ${num(s.received)} received`:"")],
    ["Blueprints",num(s.blueprints),""],
    ["Offences",num(s.crimes),s.fines?`${num(s.fines)} UEC in fines`:""],
    ["HUD notices",num(s.notifs),""],
    ["Problems",num((s.crashes||0)+(s.net_errors||0)+(s.disconnects||0)),`${num(s.crashes)} crashes · ${num(s.disconnects)} to menu · ${num(s.net_errors)} network`,"wide"],
  ];
  if(s.kills) tiles.splice(4,0,["Kills (legacy)",num(s.kills),""]);
  $("stats").innerHTML=tiles.map(([k,v,sub,cls])=>`<div class="tile ${cls||""}"><div class="v">${v}</div><div class="k">${k}</div>${sub?`<div class="s">${sub}</div>`:""}</div>`).join("");
  $("cEv").textContent=num(events.length); $("cRaw").textContent=num(s.lines);
}
function renderMeta(){
  const p=meta.path||""; $("hPath").textContent=p.length>64?"…"+p.slice(-63):p; $("hPath").title=p;
  document.title = (meta.mode==="replay"?"Replay · ":"")+"SC Log Tracker";
  renderStatus();
}
function renderStatus(){
  const dot=$("dot"), txt=$("statusText");
  if(!connected){ dot.className="dot off"; txt.textContent="Tracker not reachable"; return; }
  const ago=(Date.now()-lastLineAt)/1000;
  if(meta.waiting){ dot.className="dot idle"; txt.textContent="Waiting for Game.log"; }
  else if(meta.mode==="replay"){ dot.className=meta.replay_done?"dot idle":"dot live"; txt.textContent=meta.replay_done?"Replay finished":"Replay running"; }
  else if(lastLineAt && ago<90){ dot.className="dot live"; txt.textContent="Live"; }
  else { dot.className="dot idle"; txt.textContent=lastLineAt?`Quiet for ${ago<3600?Math.round(ago/60)+" min":Math.round(ago/3600)+" h"}`:"Live, no lines yet"; }
}
setInterval(renderStatus,5000);

/* ---------- connection ---------- */
function renderAll(){ renderMeta(); renderState(); renderChips(); renderEvents(); if(tab==="raw") renderRaw(); }
function toast(msg){ const t=$("toast"); t.textContent=msg; t.classList.add("show"); clearTimeout(t._h); t._h=setTimeout(()=>t.classList.remove("show"),4000); }
function handle(m){
  if(m.t==="snapshot"){ events=m.events; raw=m.raw; state=m.state; meta=m.meta; lastLineAt=meta.last_read||0; renderAll(); }
  else if(m.t==="batch"){
    if(m.raw.length){ raw.push(...m.raw); if(raw.length>MAX_RAW) raw.splice(0,raw.length-MAX_RAW); addRaw(m.raw); lastLineAt=Date.now(); }
    if(m.ev.length){ events.push(...m.ev); addEvents(m.ev); renderChips(); }
    state=m.state; renderState(); renderStatus();
  }
  else if(m.t==="reset"){ events=[]; raw=[]; state=m.state||{}; renderAll(); toast(m.note||"New session"); }
  else if(m.t==="meta"){ meta=m.meta; renderMeta(); }
}
function connect(){
  const es=new EventSource("/stream");
  es.onopen=()=>{ connected=true; renderStatus(); };
  es.onmessage=e=>{ try{ handle(JSON.parse(e.data)); }catch(err){ console.error(err); } };
  es.onerror=()=>{ connected=false; renderStatus(); };
}
$("btnExport").addEventListener("click",()=>{
  const blob=new Blob([JSON.stringify({exported:new Date().toISOString(),tool:"SC Log Tracker {{VERSION}}",state,events},null,1)],{type:"application/json"});
  const a=document.createElement("a"); const d=new Date();
  a.download=`sc_session_${d.toISOString().slice(0,16).replace(/[:T]/g,"-")}.json`; a.href=URL.createObjectURL(blob); a.click();
  setTimeout(()=>URL.revokeObjectURL(a.href),2000);
});
connect();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
