#!/usr/bin/env python3
"""
SC Log Tracker - a real-time viewer and history for the Star Citizen Game.log.

Follows the Game.log while you play, turns the raw lines into readable events,
keeps a complete history of all your sessions and shows everything in a local
browser dashboard:

  * live event feed with date/time range filter and session history
  * session recaps: where you were and what you did there, plus all your places
  * server history
  * automatic import of the logs Star Citizen keeps in logbackups
  * raw log view with search, filter and auto-scroll

Pure Python standard library, Python 3.9+.

Usage:
    python sc_log_tracker.py                       find the Game.log automatically
    python sc_log_tracker.py "D:\\Games\\StarCitizen\\LIVE"
    python sc_log_tracker.py --replay old.log      replay a log file (not saved to history)

Anti-cheat: the tool only reads the text files the game writes itself. No memory
access, no injection, no hooks. The log is opened briefly on every poll and
closed again, so the game can move it to logbackups on its next start.
"""

import argparse
import collections
import hashlib
import json
import os
import queue
import re
import select
import socket
import sqlite3
import string
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__version__ = "1.4.0"
APP_NAME = "SC Log Tracker"
REPO_URL = "https://github.com/DefaultHahn/sc-log-tracker"
DEFAULT_PORT = 8777
PORT_RANGE = 15
CHANNELS = ("LIVE", "PTU", "EPTU", "TECH-PREVIEW", "HOTFIX")
DEFAULT_LOG = r"C:\Program Files\Roberts Space Industries\StarCitizen\LIVE\Game.log"
# Bump when the parser produces different events, so stored history is re-imported
# from every log file that still exists.
PARSER_VERSION = 3

POLL_SECONDS = 0.25
QUIT_GRACE = 5           # seconds after the last dashboard tab closed before the app quits
READ_CHUNK = 4 * 1024 * 1024
HEAD_BYTES = 256
RAW_KEEP = 1500          # raw lines a newly connected browser receives
EVENTS_LIMIT = 20000     # max events returned for one time range
CSRF_HEADER = "X-SC-Log-Tracker"


def app_dir():
    """Folder next to the script, or next to the .exe when frozen by PyInstaller."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def default_data_dir():
    """Where settings and the history database live. Survives app updates and moves."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(Path.home(), "AppData", "Local")
        return Path(base) / "SC Log Tracker"
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(Path.home(), ".local", "share")
    return Path(base) / "sc-log-tracker"


DATA_DIR = default_data_dir()


def config_path():
    return DATA_DIR / "settings.json"


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


def pretty_actor(name):
    """NPC ids become readable ('PU_Human_Enemy_GroundCombat_NPC_ASD_soldier_6491691127623' -> 'ASD soldier').
    Player handles are returned unchanged."""
    if not name or not re.search(r"_\d{6,}$", name):
        return name
    t = re.sub(r"(_\d{6,})+$", "", name)
    t = re.sub(r"^(PU_Human_Enemy_GroundCombat_NPC_|PU_Human_Enemy_|PU_Human_|PU_|NPC_Archetypes-|NPC_)", "", t)
    parts = [x for x in re.split(r"[-_]+", t) if x and x.lower() not in ("male", "female", "human", "npc")]
    text = split_camel(" ".join(parts)).strip()
    return (text[:1].upper() + text[1:]) if text else name


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


STATION_SYSTEM = {"Everus Harbor": "Stanton", "Seraphim Station": "Stanton", "Baijini Point": "Stanton",
                  "Port Tressler": "Stanton", "Grim HEX": "Stanton", "Lorville": "Stanton", "Area18": "Stanton",
                  "Orison": "Stanton", "New Babbage": "Stanton", "Checkmate": "Pyro", "Orbituary": "Pyro",
                  "Starlight Service Station": "Pyro", "Levski": "Nyx", "Kaboos": "Nyx"}
SYSTEM_NAMES = {"Stanton", "Pyro", "Nyx", "Castra", "Magnus", "Terra"}


def system_of_code(code):
    """Star system of an internal location code: RR_P3_LEO -> Pyro, Nyx_Levski -> Nyx."""
    low = (code or "").lower()
    m = re.match(r"rr_jp_(stanton|pyro|nyx|castra|magnus|terra)", low) or \
        re.match(r"rs_ext_(stan|stanton|pyro|nyx)\w*?-\w+?_jp", low)
    if m:
        return SYSTEMS[m.group(1)]
    if low.startswith("nyx") or "levski" in low or "kaboos" in low:
        return "Nyx"
    if re.match(r"rr_p\d|rs_ext_pyro|pyro", low) or re.search(r"_pyro\d", low):
        return "Pyro"
    if re.match(r"rr_(hur|cru|arc|mic)|rs_ext_(hur|cru|arc|mic)|stanton|grimhex|lorville|area18|orison|newbabbage", low) \
            or re.search(r"_stanton\d", low):
        return "Stanton"
    return None


def system_of_name(name):
    """Star system of a readable place name, as far as it can be told."""
    if not name:
        return None
    if name in STATION_SYSTEM:
        return STATION_SYSTEM[name]
    m = re.fullmatch(r"(\w+) System", name)
    if m and m.group(1) in SYSTEM_NAMES:
        return m.group(1)
    for body_code, body in BODIES.items():
        if name == body or name.endswith(f"({body})") or name == f"Rest stop {body} (orbit)":
            return "Pyro" if body_code.startswith("pyro") else "Stanton"
    if re.match(r"PYR\d", name):
        return "Pyro"
    if re.match(r"(HUR|CRU|ARC|MIC)[- ]L\d", name):
        return "Stanton"
    return None


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


REGIONS = {"euw": "Europe", "euc": "Europe", "eun": "Europe", "use": "US East", "usw": "US West",
           "usc": "US Central", "apse": "Australia", "ape": "Asia", "apne": "Asia", "aps": "Asia",
           "sae": "South America"}
SHARD_PARTS_RE = re.compile(r"^(?P<env>[a-z]+)_(?P<region>[a-z]+?)(?P<rn>\d+)(?P<zone>[a-z]?)_(?P<build>\d+)_(?P<num>\d+)$")


def shard_info(shard):
    """pub_euw1b_12660092_110 -> region 'Europe', build '12660092', server number '110'."""
    m = SHARD_PARTS_RE.match(shard or "")
    if not m:
        return {"region": "Unknown", "code": "", "build": "", "number": ""}
    code = m["region"] + m["rn"]
    return {"region": REGIONS.get(m["region"], code.upper()), "code": code, "build": m["build"], "number": m["num"]}


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


def seconds_between(a, b):
    da, db = to_dt(a), to_dt(b)
    return int((db - da).total_seconds()) if da and db else 0


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
ENDMISSION_ID_RE = re.compile(r"MissionId\[([0-9a-fA-F-]{36})\]")
NOTIF_MISSION_RE = re.compile(r"MissionId: \[([0-9a-fA-F-]{36})\]")
NO_MISSION = "00000000-0000-0000-0000-000000000000"
PLACE_RE = re.compile(r"StatePlace::DoPlace\] '([^']+)' \[\d+\] placed '([^']+?)(?:_\d{6,})?' \[\d+\] "
                      r"(?:in lootable container|on entity) '([^']+)'")
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
BRANCH_RE = re.compile(r"Branch:\s*sc-alpha-(\S+)")
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


KEYCARDS = {"weaponcache": "weapon cache", "securestorage": "secure storage", "bunkercard": "bunker",
            "security": "security", "maintenance": "maintenance"}


def classify_placement(item, container):
    """What putting an item somewhere means: (kind, title, detail), or None if it's nothing special."""
    it, ct = item.lower(), re.sub(r"([_-]\d{6,})+$", "", container.lower())
    if it.startswith("fuse"):
        where = ("contested zone relay" if "_cz_" in ct else "door" if "door" in ct
                 else "fuse box" if "fusebox" in ct or "lever" in ct else "relay")
        return "fuse", "Fuse inserted", where
    if "hackingchip" in it:
        m = re.search(r"access_level_(\w+?)(?:_\d+)?$", ct)
        lvl = m.group(1) if m else ""
        level = (f"level {lvl} access" if lvl.isdigit() else f"{lvl} access" if lvl
                 else "comm array" if "commarray" in ct else "")
        return "chip", "Hacking chip used", level
    if "keycard" in it:
        m = re.search(r"access_(?:[a-z]+_)*?([a-z]+)$", ct)
        return "keycard", "Keycard used", KEYCARDS.get(m.group(1), split_camel(m.group(1))) if m else ""
    if "harddrive" in it:
        return "drive", "Hard drive inserted", "ASD facility" if "asd" in it else ""
    if re.search(r"se(?:r)?verrack|serverblade", ct):           # the game spells it "SeverRack" too
        return "blade", "Server blade inserted", "ASD facility" if "delving" in ct else ""
    if "rockcracker" in ct:
        return "crystal", "Crystal inserted", "Rockcracker"
    if "vlk_egg" in it:
        return "egg", "Valakkar egg delivered", ""
    if it.startswith("mining_gadget"):
        return "gadget", "Mining gadget placed", pretty_item(item[len("Mining_Gadget_"):])
    return None


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
        self.mission_names = {}     # mission id -> contract name, to name the outcome
        self.qt_real = False        # the quantum target is a real place (not a marker)
        self.state = {
            "handle": None, "shard": None, "channel": None, "version": None,
            "first_ts": None, "last_ts": None,
            "location": None, "jurisdiction": None,
            "armistice": None, "monitored": None,
            "ship": None, "ship_owner": None, "pilot_ship": None,
            "qt_target": None, "region": None,
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
    def _ev(self, out, ts, cat, title, detail="", level="info", raw="", x=None):
        self.seq += 1
        self.state["events"] += 1
        e = {"id": self.seq, "ts": ts, "c": cat, "ti": title, "d": detail,
             "lv": level, "n": self.line_no, "raw": raw[:700]}
        if x is not None:
            e["x"] = x                       # machine-readable value, e.g. the shard id
        out.append(e)

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

    def _set_location(self, loc, ts, out, raw, system=None):
        if loc and self._changed("loc", loc):
            self.state["location"] = loc
            self._ev(out, ts, "travel", "Location", loc, "info", raw, x=system or system_of_name(loc))

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
                    self._notif(p["text"] + " " + em.group(1), em.group(2), p["ts"], p["raw"], out,
                                self._mission_id(line))
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
                self._notif(nm.group(1), nm.group(2), ts, line, out, self._mission_id(line))
            else:
                start = line.index(NOTIF_START) + len(NOTIF_START)
                self.pend = {"text": line[start:], "ts": ts, "raw": line, "n": 0}
            return out

        self._other(line, ts, out)
        return out

    @staticmethod
    def _mission_id(line):
        m = NOTIF_MISSION_RE.search(line)
        return m.group(1).lower() if m and m.group(1) != NO_MISSION else None

    # -- HUD notifications ------------------------------------------------------
    def _notif(self, text, nid, ts, raw, out, mission=None):
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
            if mission and name and kind != "Available":
                self.mission_names[mission] = name
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
        m = re.match(r"Objective Failed:\s*(.*)", t)
        if m:
            return ev("mission", "Objective failed", m.group(1), "bad")
        m = re.match(r"Contract Withdrawn:\s*(.*)", t)
        if m:
            return ev("mission", "Contract withdrawn", m.group(1), "warn")
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
        m = re.match(r"Item Bricking Initiated: Your (.+?)\s+(?:is|are) bricking", t)
        if m:
            return ev("notice", "Item bricking", m.group(1), "warn")
        m = re.match(r"Item Bricked: Your (.+?)\s+(?:is|are) now bricked", t)
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
        if t.startswith("You have left the party"):
            return ev("social", "Left party")
        m = re.match(r"Invitation Declined (\S+) has declined", t)
        if m:
            return ev("social", "Invitation declined", m.group(1), "warn")
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
            if m:
                shard = m.group(1)
                S["shard"], S["region"] = shard, shard_info(shard)["region"]
                self._ev(out, ts, "session", "Joined server", f"{shard} · {S['region']}", "info", line, x=shard)
            return
        if all(k in line for k in PU_JOIN):
            return ev("session", "Entered the universe", "persistent universe loaded", "good")

        # --- location and quantum travel ---
        if "<RequestLocationInventory>" in line:
            m = LOCINV_RE.search(line)
            if m and (S["handle"] is None or m.group(1) == S["handle"]):
                self._set_location(pretty_loc(m.group(2)), ts, out, line, system_of_code(m.group(2)))
            return
        if "Projected Start Location is" in line:
            m = ROUTE_RE.search(line)
            if m:
                self._set_location(m.group(1).strip(), ts, out, line)
            return
        if QT_SELECT in line:
            m = QT_DEST_RE.search(line)
            dest, self.qt_real = pretty_dest(m.group(1)) if m else ("", False)
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
                S["qt_target"], self.qt_real = pretty_dest(m.group(1))
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
            self._ev(out, ts, "travel", "Quantum jump arrived", detail, "good", line,
                     x=dest if dest and self.qt_real else None)
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
        if "StatePlace::DoPlace]" in line:
            m = PLACE_RE.search(line)
            if m and (S["handle"] is None or m.group(1) == S["handle"]):
                act = classify_placement(m.group(2), m.group(3))
                if act:
                    kind, title, detail = act
                    self._ev(out, ts, "activity", title, detail, "info", line, x=kind)
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
            outcome = COMPLETION.get(low, ctype.lower())
            im = ENDMISSION_ID_RE.search(line)
            name = self.mission_names.get(im.group(1).lower()) if im else None
            return self._ev(out, ts, "mission", "Mission ended", f"{outcome}: {name}" if name else outcome,
                            level, line, x=outcome)

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
                return self._ev(out, ts, "economy", "Bought cargo", f"{fmt_num(price)} aUEC{at}", "info", line,
                                x=f"{price:.0f}")
            im = ITEMBUY_RE.search(line)
            if im:
                price, item, qty = float(im.group(1)), pretty_item(im.group(2)), int(im.group(3))
                S["spend"] += price
                count = f"{qty}x " if qty > 1 else ""
                return self._ev(out, ts, "economy", "Purchase", f"{count}{item} for {fmt_num(price)} aUEC{at}",
                                "info", line, x=f"{price:.0f}")
            return ev("economy", "Purchase", shop)
        if "RmShopFlowResponse" in line:
            m = SHOPRESP_RE.search(line)
            if m and m.group(1) != "Success":
                return ev("economy", "Purchase failed", m.group(1), "bad")
            return

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
                    ev("combat", "Kill", f"{pretty_actor(victim)} with {wpn} ({dtype})", "good")
                elif me and victim == me:
                    S["deaths"] += 1
                    ev("combat", "Killed", f"by {pretty_actor(killer)} with {wpn} ({dtype})", "bad")
                # deaths of everyone else nearby were logged too: too noisy to show
            return
        if "<Vehicle Destruction>" in line:
            m = VDESTROY_RE.search(line)
            if m:
                veh, _zone, driver, _from, lvl_to, cause = m.groups()
                me = S["handle"]
                if me and me in (driver, cause):
                    what = "destroyed" if lvl_to == "2" else "disabled"
                    ev("combat", f"Vehicle {what}", f"{pretty_class(veh)} (pilot: {driver}, by {cause})",
                       "bad" if driver == me else "good")
            return

        if S["version"] is None and ("FileVersion" in line or "ProductVersion" in line):
            m = FULLVER_RE.search(line)
            if m:
                S["version"] = m.group(1)
                if not m.group(1).startswith("1.0."):  # hotfix builds report 1.0.x, see Branch below
                    return ev("session", "Game version", m.group(1))
                return
        if "Branch: sc-alpha-" in line and (S["version"] is None or S["version"].startswith("1.0.")):
            m = BRANCH_RE.search(line)
            if m:
                S["version"] = m.group(1)
                return ev("session", "Game version", m.group(1))
        if S["channel"] is None and "Bin64" in line:
            m = EXE_CHANNEL_RE.search(line)
            if m:
                S["channel"] = m.group(1).upper()


# ---------------------------------------------------------------------------
# History store (SQLite)
# ---------------------------------------------------------------------------

def session_key(first_line):
    """Stable id for one game session: its first log line (timestamp + backup name)."""
    if isinstance(first_line, str):
        first_line = first_line.encode("utf-8", "replace")
    return hashlib.sha1(first_line.strip()).hexdigest()[:16]


def first_line_of(path):
    """First complete line of a file, or None if there is none yet."""
    try:
        with open(path, "rb") as f:
            line = f.readline(8192)
    except OSError:
        return None
    return line if line.endswith(b"\n") else None


class Store:
    """All events of all sessions, so the history survives Star Citizen deleting old logs."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS sessions(
        id TEXT PRIMARY KEY, path TEXT, first_ts TEXT, last_ts TEXT,
        size INTEGER DEFAULT 0, lines INTEGER DEFAULT 0, events INTEGER DEFAULT 0,
        handle TEXT, version TEXT, channel TEXT, parser INTEGER DEFAULT 0, updated REAL);
    CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY, sid TEXT NOT NULL, seq INTEGER NOT NULL,
        ts TEXT, c TEXT, ti TEXT, d TEXT, lv TEXT, n INTEGER, raw TEXT, x TEXT,
        UNIQUE(sid, seq));
    CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
    CREATE INDEX IF NOT EXISTS sessions_first ON sessions(first_ts);
    """

    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            if self.path != ":memory:":
                self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(self.SCHEMA)
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(events)")}
            if "x" not in cols:                  # databases from 1.1.0
                self.db.execute("ALTER TABLE events ADD COLUMN x TEXT")
            self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    def begin_session(self, sid, path):
        """Register a session. Events from an older parser version are dropped and re-parsed."""
        with self.lock:
            row = self.db.execute("SELECT parser FROM sessions WHERE id=?", (sid,)).fetchone()
            if row and row["parser"] != PARSER_VERSION:
                self.db.execute("DELETE FROM events WHERE sid=?", (sid,))
                self.db.execute("UPDATE sessions SET size=0 WHERE id=?", (sid,))
            self.db.execute(
                "INSERT INTO sessions(id, path, parser, updated) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET path=excluded.path, parser=excluded.parser, updated=excluded.updated",
                (sid, str(path), PARSER_VERSION, time.time()))
            self.db.commit()

    def add_events(self, sid, events):
        """Store events (duplicates are ignored). Returns the newly stored ones with their database id."""
        new = []
        if not events:
            return new
        with self.lock:
            cur = self.db.cursor()
            for e in events:
                cur.execute(
                    "INSERT OR IGNORE INTO events(sid, seq, ts, c, ti, d, lv, n, raw, x) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sid, e["id"], e["ts"], e["c"], e["ti"], e["d"], e["lv"], e["n"], e["raw"], e.get("x")))
                if cur.rowcount:
                    new.append(dict(e, id=cur.lastrowid, seq=e["id"], sid=sid))
            self.db.commit()
        return new

    def update_session(self, sid, state, size=None):
        with self.lock:
            count = self.db.execute("SELECT COUNT(*) FROM events WHERE sid=?", (sid,)).fetchone()[0]
            self.db.execute(
                "UPDATE sessions SET first_ts=?, last_ts=?, lines=?, events=?, handle=?, version=?, channel=?, "
                "size=COALESCE(?, size), updated=? WHERE id=?",
                (state.get("first_ts"), state.get("last_ts"), state.get("lines", 0), count, state.get("handle"),
                 state.get("version"), state.get("channel"), size, time.time(), sid))
            self.db.commit()

    def session(self, sid):
        with self.lock:
            row = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        return dict(row) if row else None

    def sessions(self, limit=2000):
        with self.lock:
            rows = self.db.execute(
                "SELECT id, path, first_ts, last_ts, events, handle, version, channel FROM sessions "
                "WHERE events > 0 AND first_ts IS NOT NULL ORDER BY first_ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def events(self, frm=None, to=None, limit=EVENTS_LIMIT):
        """Events between two ISO timestamps (UTC), oldest first. Returns (events, total in range)."""
        where, args = ["ts IS NOT NULL"], []
        if frm:
            where.append("ts >= ?")
            args.append(frm)
        if to:
            where.append("ts <= ?")
            args.append(to)
        cond = " AND ".join(where)
        with self.lock:
            total = self.db.execute(f"SELECT COUNT(*) FROM events WHERE {cond}", args).fetchone()[0]
            rows = self.db.execute(
                f"SELECT id, sid, seq, ts, c, ti, d, lv, n, raw, x FROM events WHERE {cond} "
                "ORDER BY ts DESC, id DESC LIMIT ?", args + [limit]).fetchall()
        return [dict(r) for r in reversed(rows)], total

    VISIT_ENDS = ("Exited to menu", "Disconnected from server", "Disconnected for inactivity",
                  "Game closed", "Crash!")
    REJOIN_GAP = 300                         # same server again within 5 minutes counts as one visit

    def server_visits(self, frm=None, to=None):
        """Server visits overlapping the time range, newest first.
        A visit runs from joining a shard until leaving it, joining another one, or the session's end."""
        cond, args = ["s.events > 0", "s.first_ts IS NOT NULL"], []
        if to:
            cond.append("s.first_ts <= ?")
            args.append(to)
        if frm:
            cond.append("s.last_ts >= ?")
            args.append(frm)
        ends = ",".join("?" * len(self.VISIT_ENDS))
        with self.lock:
            rows = self.db.execute(
                "SELECT e.sid, e.ts, e.ti, e.x, s.last_ts FROM events e JOIN sessions s ON s.id = e.sid "
                f"WHERE {' AND '.join(cond)} AND e.ts IS NOT NULL "
                f"AND (e.ti = 'Joined server' OR e.ti IN ({ends}) OR e.ti LIKE 'Connection error%') "
                "ORDER BY e.sid, e.ts, e.id", args + list(self.VISIT_ENDS)).fetchall()
        visits, current, last_sid, session_end = [], None, None, None
        for r in rows:
            if r["sid"] != last_sid:
                if current:
                    current.update(end=session_end, left=None, open=True)
                current, last_sid, session_end = None, r["sid"], r["last_ts"]
            if r["ti"] == "Joined server":
                shard = r["x"] or ""
                if current:
                    current.update(end=r["ts"], left="Moved to another server")
                prev = visits[-1] if visits and visits[-1]["sid"] == r["sid"] else None
                if (prev and prev["shard"] == shard and prev["end"]
                        and seconds_between(prev["end"], r["ts"]) <= self.REJOIN_GAP):
                    prev.update(end=None, left=None, rejoins=prev["rejoins"] + 1)
                    current = prev
                else:
                    current = {"sid": r["sid"], "shard": shard, **shard_info(shard), "start": r["ts"],
                               "end": None, "left": None, "rejoins": 0, "open": False}
                    visits.append(current)
            elif current:
                current.update(end=r["ts"], left=r["ti"])
                current = None
        if current:
            current.update(end=session_end, left=None, open=True)
        out = []
        for v in visits:
            v["end"] = v["end"] or v["start"]
            # overlap with the range; a visit that only touches its edge doesn't count
            if to and (v["start"] > to or (v["start"] == to and v["end"] > to)):
                continue
            if frm and (v["end"] < frm or (v["end"] == frm and v["start"] < frm)):
                continue
            v["seconds"] = max(0, seconds_between(v["start"], v["end"]))
            out.append(v)
        out.sort(key=lambda v: v["start"], reverse=True)
        return out

    def recap(self, frm=None, to=None):
        """Recaps of the sessions overlapping the time range (newest first) and the places in them."""
        cond, args = ["s.events > 0", "s.first_ts IS NOT NULL"], []
        if to:
            cond.append("s.first_ts <= ?")
            args.append(to)
        if frm:
            cond.append("s.last_ts >= ?")
            args.append(frm)
        where = " AND ".join(cond)
        with self.lock:
            sessions = [dict(r) for r in self.db.execute(
                f"SELECT s.id, s.first_ts, s.last_ts, s.handle, s.version FROM sessions s WHERE {where} "
                "ORDER BY s.first_ts DESC", args)]
            rows = self.db.execute(
                "SELECT e.sid, e.ts, e.c, e.ti, e.d, e.x FROM events e JOIN sessions s ON s.id = e.sid "
                f"WHERE {where} AND e.ts IS NOT NULL AND e.c != 'notice' ORDER BY e.sid, e.seq", args).fetchall()
        by_sid = collections.defaultdict(list)
        for r in rows:
            by_sid[r["sid"]].append(r)
        recaps = [build_recap(s, by_sid.get(s["id"], [])) for s in sessions]
        return recaps, places_overview(recaps)

    def stats(self):
        with self.lock:
            row = self.db.execute(
                "SELECT COUNT(*) AS sessions, COALESCE(SUM(events), 0) AS events, MIN(first_ts) AS oldest "
                "FROM sessions WHERE events > 0").fetchone()
        out = dict(row)
        try:
            out["bytes"] = os.path.getsize(self.path) if self.path != ":memory:" else 0
        except OSError:
            out["bytes"] = 0
        return out


# ---------------------------------------------------------------------------
# Session recap: where you were and what you did there
# ---------------------------------------------------------------------------

RECAP_MIN_STAY = 90          # shorter stops where nothing happened are left out
SPAWN_BLIP = 600             # a place reported right after (re)spawning and left again within this
SPAWN_WINDOW = 150           # ... many seconds, in between two stays at the same place, is a spawn artifact
PAIR_WINDOW = 45             # a HUD message and the mission-end line within this many seconds are one outcome
PARTY_TITLES = ("Party member joined", "Party member online", "Jumping to you", "Party invite", "Party launch",
                "Came aboard", "New party leader")
BUY_RE = re.compile(r"^(?:(\d+)x )?(.+?) for [\d,]+ aUEC")
OUTCOME_LABEL = {"accepted": "Accepted", "completed": "Completed", "failed": "Failed",
                 "abandoned": "Abandoned", "withdrawn": "Withdrawn"}


def _chapter(place, system, start, via=None, spawned=False):
    return {"place": place, "system": system, "start": start, "end": start, "via": via, "spawned": spawned,
            "buys": {}, "spent": 0.0, "contracts": [], "flown": [], "boarded": [], "jumps": 0,
            "acts": {}, "deaths": [], "downed": 0, "injuries": 0, "medbed": 0, "collisions": 0,
            "kills": 0, "law": {}, "servers": [], "reconnects": 0, "blueprints": 0, "crash": None, "other": []}


def _add_unique(items, value):
    if value and value not in items:
        items.append(value)


def _count(d, key, n=1):
    d[key] = d.get(key, 0) + n


def _busy(ch):
    return any(ch[k] for k in ("buys", "contracts", "flown", "boarded", "jumps", "acts", "deaths", "downed",
                               "injuries", "medbed", "collisions", "kills", "law", "blueprints", "crash", "other"))


def _merge(a, b):
    a["end"] = max(a["end"], b["end"])
    a["spent"] += b["spent"]
    for k in ("jumps", "downed", "injuries", "medbed", "collisions", "kills", "reconnects", "blueprints"):
        a[k] += b[k]
    for k in ("buys", "acts", "law"):
        for key, n in b[k].items():
            _count(a[k], key, n)
    for k in ("contracts", "deaths", "servers", "other"):
        a[k].extend(b[k])
    for k in ("flown", "boarded"):
        for v in b[k]:
            _add_unique(a[k], v)
    a["crash"] = a["crash"] or b["crash"]


def _times(n):
    return f" ({n}×)" if n > 1 else ""


def _lines(ch):
    """The readable lines for one stop: what you did there."""
    out = []

    def add(cat, text):
        out.append({"c": cat, "t": text})

    if ch["flown"]:
        add("ship", "Flew " + ", ".join(ch["flown"]))
    if ch["boarded"]:
        add("ship", "Aboard " + ", ".join(re.sub(r"\s+", " ", b) for b in ch["boarded"]))
    if ch["jumps"]:
        add("travel", f"{ch['jumps']} quantum jump{'s' if ch['jumps'] > 1 else ''}")
    grouped = {}
    for c in ch["contracts"]:
        _count(grouped, (c["outcome"], c["name"] or ""))
    for (outcome, name), n in grouped.items():
        label = OUTCOME_LABEL.get(outcome, outcome.capitalize())
        add("mission", (f"{label}: {name}" if name else f"Contract {outcome}") + _times(n))
    if ch["buys"]:
        items = [f"{n}× {item}" if n > 1 else item for item, n in ch["buys"].items()]
        more = f" and {len(items) - 3} more" if len(items) > 3 else ""
        add("economy", f"Bought {', '.join(items[:3])}{more} · {fmt_num(ch['spent'])} aUEC")
    for (title, detail), n in ch["acts"].items():
        add("activity", (f"{n}× " if n > 1 else "") + title + (f" · {detail}" if detail else ""))
    if ch["blueprints"]:
        add("economy", f"{ch['blueprints']} blueprint{'s' if ch['blueprints'] > 1 else ''} received")
    for text in ch["other"]:
        add("economy", text)
    if ch["kills"]:
        add("combat", f"{ch['kills']} kill{'s' if ch['kills'] > 1 else ''}")
    if len(ch["deaths"]) == 1:
        add("combat", "Died" + (f" ({ch['deaths'][0]})" if ch["deaths"][0] else ""))
    elif ch["deaths"]:
        add("combat", f"Died {len(ch['deaths'])}×")
    if ch["downed"]:
        add("combat", f"Downed, emergency services called{_times(ch['downed'])}")
    if ch["injuries"]:
        add("combat", f"{ch['injuries']} injur{'ies' if ch['injuries'] > 1 else 'y'}")
    if ch["medbed"]:
        add("combat", f"Treated in a med bed{_times(ch['medbed'])}")
    if ch["collisions"]:
        add("combat", f"Fatal collision{_times(ch['collisions'])}")
    for title, n in ch["law"].items():
        add("law", title + _times(n))
    if ch["servers"]:
        regions = sorted({shard_info(x)["region"] for x in ch["servers"]})
        add("session", f"Changed server{_times(len(ch['servers']))} · {', '.join(regions)}")
    if ch["reconnects"]:
        add("session", f"Reconnected to the same server{_times(ch['reconnects'])}")
    if ch["crash"]:
        add("error", ch["crash"])
    return out


def build_recap(sess, events):
    """Summary and stops of one session from its stored events."""
    me = sess.get("handle")
    chapters, outcomes = [], []
    cur = _chapter(None, None, sess["first_ts"])
    servers, flown, boarded, party, acts = {}, [], [], [], {}
    totals = {"spent": 0.0, "buys": 0, "accepted": 0, "jumps": 0, "deaths": 0, "downed": 0,
              "injuries": 0, "crashes": 0, "blueprints": 0}
    last_shard, last_spawn = None, None

    def outcome(kind, name, ts):
        for o in reversed(outcomes):
            if seconds_between(o["ts"], ts) > PAIR_WINDOW:
                break
            if o["outcome"] == kind and (not o["name"] or not name or o["name"] == name):
                o["name"] = o["name"] or name
                return
        o = {"outcome": kind, "name": name, "ts": ts}
        outcomes.append(o)
        cur["contracts"].append(o)

    for e in events:
        ti, d, x, ts, cat = e["ti"], e["d"] or "", e["x"], e["ts"], e["c"]
        if ti == "Location" or (ti == "Quantum jump arrived" and x):
            place = d if ti == "Location" else x
            if ti != "Location":
                totals["jumps"] += 1
            if place and place != cur["place"]:
                cur["end"] = ts
                chapters.append(cur)
                system = (x if ti == "Location" else None) or system_of_name(place)
                spawned = bool(last_spawn) and seconds_between(last_spawn, ts) <= SPAWN_WINDOW
                cur = _chapter(place, system, ts, None if ti == "Location" else "quantum", spawned)
            continue
        if ti == "Quantum jump arrived":
            totals["jumps"] += 1
            cur["jumps"] += 1
        elif ti in ("Purchase", "Bought cargo"):
            try:
                price = float(x)
            except (TypeError, ValueError):
                m = re.search(r"([\d,]+) aUEC", d)
                price = float(m.group(1).replace(",", "")) if m else 0.0
            m = BUY_RE.match(d) if ti == "Purchase" else None
            item = m.group(2) if m else ("cargo" if ti == "Bought cargo" else (d or "something"))
            _count(cur["buys"], item, int(m.group(1)) if m and m.group(1) else 1)
            cur["spent"] += price
            totals["spent"] += price
            totals["buys"] += 1
        elif ti == "Contract accepted":
            totals["accepted"] += 1
            cur["contracts"].append({"outcome": "accepted", "name": d, "ts": ts})
        elif ti == "Mission ended":
            kind, _, name = d.partition(": ")
            outcome(x or kind, name, ts)
        elif ti in ("Contract complete", "Contract failed"):
            outcome("completed" if ti == "Contract complete" else "failed", d, ts)
        elif ti in ("Took the controls", "Left pilot seat"):
            _add_unique(cur["flown"], d)
            _add_unique(flown, d)
        elif ti == "Boarded":
            _add_unique(cur["boarded"], d)
            _add_unique(boarded, re.sub(r"\s+", " ", d))
        elif cat == "activity":
            _count(cur["acts"], (ti, d))
            _count(acts, ti)
        elif ti in ("You died", "Killed"):
            cur["deaths"].append(d)
            totals["deaths"] += 1
        elif ti in ("Emergency services en route", "Incapacitated"):
            cur["downed"] += 1
            totals["downed"] += 1
        elif ti.endswith(" injury"):
            cur["injuries"] += 1
            totals["injuries"] += 1
        elif ti == "Treated in med bed":
            cur["medbed"] += 1
        elif ti == "Fatal collision":
            cur["collisions"] += 1
        elif ti == "Kill":
            cur["kills"] += 1
        elif cat == "law":
            _count(cur["law"], ti)
        elif ti == "Joined server":
            shard = x or d.split(" · ")[0]
            servers.setdefault(shard, shard_info(shard)["region"])
            if last_shard == shard:
                cur["reconnects"] += 1
            elif last_shard:
                cur["servers"].append(shard)
            last_shard, last_spawn = shard, ts
        elif ti == "Entered the universe":
            last_spawn = ts
        elif ti == "Blueprint received":
            cur["blueprints"] += 1
            totals["blueprints"] += 1
        elif ti == "Crash!":
            totals["crashes"] += 1
            cur["crash"] = "Star Citizen crashed"
        elif ti == "Crash cause":
            cur["crash"] = f"Star Citizen crashed ({d})"
        elif ti in ("Refinery order complete", "Money received", "Money sent"):
            cur["other"].append(f"{ti}: {d}" if d else ti)
        elif cat == "social" and ti in PARTY_TITLES and d:
            name = re.sub(r"^(from|started by)\s+", "", d).split()[0]
            if name != me and name not in ("invites",):
                _add_unique(party, name)
    cur["end"] = sess["last_ts"] or cur["start"]
    chapters.append(cur)

    kept = []
    for i, ch in enumerate(chapters):
        secs = seconds_between(ch["start"], ch["end"])
        if ch["place"] is None and i + 1 < len(chapters) and secs < SPAWN_BLIP:
            # what happened while loading in belongs to the first place you're at
            _merge(chapters[i + 1], ch)
            continue
        if not _busy(ch) and (ch["place"] is None or secs < RECAP_MIN_STAY):
            if kept:
                kept[-1]["end"] = max(kept[-1]["end"], ch["end"])
            continue
        nxt = chapters[i + 1]["place"] if i + 1 < len(chapters) else None
        if kept and ch["spawned"] and secs < SPAWN_BLIP and nxt == kept[-1]["place"]:
            # After a server change the game briefly reports another place (e.g. your home
            # station) before the real one: count it as the place around it.
            _merge(kept[-1], ch)
            continue
        if kept and kept[-1]["place"] == ch["place"]:
            _merge(kept[-1], ch)
        else:
            kept.append(ch)

    stops, route = [], []
    for ch in kept:
        stops.append({"place": ch["place"], "system": ch["system"], "start": ch["start"], "end": ch["end"],
                      "seconds": max(0, seconds_between(ch["start"], ch["end"])), "via": ch["via"],
                      "lines": _lines(ch), "spent": round(ch["spent"]),
                      "acts": {t: sum(n for (tt, _), n in ch["acts"].items() if tt == t) for t, _ in ch["acts"]},
                      "contracts": sum(1 for c in ch["contracts"] if c["outcome"] == "accepted")})
        if ch["place"] and not ch["place"].endswith(" System") and (not route or route[-1] != ch["place"]):
            route.append(ch["place"])
    done = {k: sum(1 for o in outcomes if o["outcome"] == k) for k in ("completed", "failed", "abandoned")}
    summary = {"servers": [{"shard": k, "region": v} for k, v in servers.items()],
               "flown": flown, "boarded": boarded, "party": party, "acts": acts, "route": route,
               "spent": round(totals["spent"]), "buys": totals["buys"], "jumps": totals["jumps"],
               "deaths": totals["deaths"], "downed": totals["downed"], "injuries": totals["injuries"],
               "crashes": totals["crashes"], "blueprints": totals["blueprints"],
               "contracts": {"accepted": totals["accepted"], **done}}
    return {"sid": sess["id"], "start": sess["first_ts"], "end": sess["last_ts"],
            "seconds": max(0, seconds_between(sess["first_ts"], sess["last_ts"])),
            "version": sess.get("version"), "summary": summary, "chapters": stops}


def places_overview(recaps):
    """Every place in these sessions: visits, time there and what you did there."""
    places = {}
    for r in recaps:
        for ch in r["chapters"]:
            name = ch["place"]
            if not name or name.endswith(" System"):
                continue
            p = places.setdefault(name, {"place": name, "system": ch["system"], "visits": 0, "sessions": set(),
                                         "seconds": 0, "first": ch["start"], "last": ch["end"], "spent": 0,
                                         "contracts": 0, "acts": {}})
            p["visits"] += 1
            p["sessions"].add(r["sid"])
            p["seconds"] += ch["seconds"]
            p["first"], p["last"] = min(p["first"], ch["start"]), max(p["last"], ch["end"])
            p["spent"] += ch["spent"]
            p["contracts"] += ch["contracts"]
            p["system"] = p["system"] or ch["system"]
            for k, n in ch["acts"].items():
                _count(p["acts"], k, n)
    out = []
    for p in places.values():
        p["sessions"] = len(p["sessions"])
        out.append(p)
    out.sort(key=lambda p: p["seconds"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Hub: live state, history store and connected browsers
# ---------------------------------------------------------------------------

class Hub:
    def __init__(self, store, mode, quiet=False):
        self.lock = threading.Lock()
        self.store = store
        self.mode = mode
        self.quiet = quiet
        self.parser = Parser()
        self.sid = None
        self.announced = None
        self.raw = collections.deque(maxlen=RAW_KEEP)
        self.clients = set()
        self.had_client = False      # a dashboard was opened at least once
        self.empty_since = None      # when the last dashboard tab went away
        self.source = None
        self.importer = None
        self.meta = {"path": None, "mode": mode, "waiting": False, "needs_setup": False, "last_read": 0,
                     "history_done": False, "tool": __version__, "import": None, "sid": None}

    # -- browsers ---------------------------------------------------------------
    def snapshot(self):
        return {"t": "snapshot", "v": __version__, "raw": list(self.raw), "state": dict(self.parser.state),
                "meta": dict(self.meta)}

    def subscribe(self):
        q = queue.Queue(maxsize=3000)
        with self.lock:
            self.clients.add(q)
            self.had_client, self.empty_since = True, None
            return q, self.snapshot()

    def unsubscribe(self, q):
        with self.lock:
            self._drop(q)

    def _drop(self, q):
        self.clients.discard(q)
        if not self.clients and self.empty_since is None:
            self.empty_since = time.time()

    def game_running(self):
        """True while Star Citizen is writing to the followed log (a line in the last few minutes)."""
        if self.mode != "live":
            return False
        last = to_dt(self.parser.state.get("last_ts"))
        return bool(last and (datetime.now(timezone.utc) - last).total_seconds() < 180)

    def dashboard_closed_for(self):
        """Seconds since the last dashboard tab was closed, or None while one is open (or none ever was)."""
        with self.lock:
            if not self.had_client or self.clients or self.empty_since is None:
                return None
            return time.time() - self.empty_since

    def _send(self, q, msg):
        try:
            q.put_nowait(msg)
        except queue.Full:
            q.dead = True                # browser too slow: let it reconnect
            self._drop(q)

    def _broadcast(self, msg):
        for q in list(self.clients):
            self._send(q, msg)

    def broadcast(self, msg):
        with self.lock:
            self._broadcast(msg)

    def set_meta(self, src=None, **kw):
        if not self._mine(src):
            return
        with self.lock:
            if any(self.meta.get(k) != v for k, v in kw.items()):
                self.meta.update(kw)
                self._broadcast({"t": "meta", "meta": dict(self.meta)})

    def sessions_changed(self):
        self.broadcast({"t": "sessions"})

    # -- sources ------------------------------------------------------------------
    def _mine(self, src):
        """Ignore stragglers from a tailer that was replaced by switch_log()."""
        return src is None or src is self.source

    def switch_log(self, path, remember=True):
        path = Path(path)
        with self.lock:
            old_src, old_imp = self.source, self.importer
            self.source = self.importer = None
        for t in (old_src, old_imp):
            if t:
                t.stop()
        with self.lock:
            self.parser = Parser()
            self.raw.clear()
            self.sid = self.announced = None
            self.meta.update(path=str(path), needs_setup=False, waiting=False, history_done=False, sid=None)
            self.source = FileTailer(path, self)
            self.importer = Importer(path.parent / "logbackups", self)
            self._broadcast({"t": "reset", "note": None, "state": dict(self.parser.state), "meta": dict(self.meta)})
        if remember:
            save_config({**load_config(), "log": str(path)})
        con(f"Game.log: {path}")
        self.source.start()
        self.importer.start()

    def begin_session(self, sid, path, src=None):
        if not self._mine(src):
            return
        self.store.begin_session(sid, path)
        with self.lock:
            self.sid = sid
            self.meta["sid"] = sid

    def reset(self, note, src=None):
        if not self._mine(src):
            return
        with self.lock:
            self.parser = Parser()
            self.raw.clear()
            self.sid = self.announced = None
            self.meta["sid"] = None
            self._broadcast({"t": "reset", "note": note, "state": dict(self.parser.state), "meta": dict(self.meta)})
        if not self.quiet:
            con(f"\n=== {note} ===\n", "96")

    def feed(self, lines, history=False, size=None, src=None):
        if not self._mine(src):
            return
        parsed, new_raw = [], []
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
                parsed.extend(evs)
            sid, state = self.sid, dict(self.parser.state)
        new = self.store.add_events(sid, parsed) if sid else []
        if sid:
            self.store.update_session(sid, state, size)
        with self.lock:
            self.meta["last_read"] = int(time.time() * 1000)
            if not history:
                self._broadcast({"t": "batch", "ev": new, "raw": new_raw, "state": state,
                                 "lr": self.meta["last_read"]})
                if new and self.announced != sid:
                    self.announced = sid
                    self._broadcast({"t": "sessions"})
        if not history and not self.quiet:
            for e in new:
                print_event(e)

    def history_done(self, src=None):
        if not self._mine(src):
            return
        with self.lock:
            self.meta["history_done"] = True
            self.announced = self.sid
            snap = self.snapshot()
            for q in list(self.clients):
                self._send(q, snap)
                self._send(q, {"t": "sessions"})
            st = self.parser.state
        if not self.quiet and self.mode == "live":
            con(f"Read the current session so far: {fmt_num(st['lines'])} lines, "
                f"{fmt_num(st['events'])} events. Now following live ...", "92")

    def set_import(self, src=None, **info):
        if src is not None and src is not self.importer:
            return
        with self.lock:
            self.meta["import"] = info
            self._broadcast({"t": "meta", "meta": dict(self.meta)})


# ---------------------------------------------------------------------------
# Sources: live tailer, history importer and replay
# ---------------------------------------------------------------------------

class FileTailer(threading.Thread):
    daemon = True

    def __init__(self, path, hub):
        super().__init__(name="tailer")
        self.path = Path(path)
        self.hub = hub
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        pos, buf, head, sid = 0, b"", b"", None
        catching_up = True
        hub, wait = self.hub, self.stop_event.wait
        while not self.stop_event.is_set():
            try:
                size = os.path.getsize(self.path)
            except OSError:
                hub.set_meta(src=self, waiting=True)
                if catching_up:
                    catching_up = False
                    hub.history_done(src=self)
                wait(1.0)
                continue
            hub.set_meta(src=self, waiting=False)
            data = b""
            try:
                # open and close on every poll (see module docstring)
                with open(self.path, "rb") as f:
                    h = f.read(HEAD_BYTES)
                    n = min(len(h), len(head))
                    if pos and (size < pos or h[:n] != head[:n]):
                        pos, buf, head, sid = 0, b"", b"", None
                        catching_up = False
                        hub.reset("New game session detected (Game.log was recreated)", src=self)
                    if len(h) > len(head):
                        head = h
                    if size > pos:
                        f.seek(pos)
                        data = f.read(min(size - pos, READ_CHUNK))
            except OSError:
                wait(POLL_SECONDS)
                continue

            if data:
                pos += len(data)
                data = buf + data
                parts = data.split(b"\n")
                buf = parts.pop()
                if parts and sid is None:
                    sid = session_key(parts[0])
                    hub.begin_session(sid, self.path, src=self)
                lines = [p.decode("utf-8", "replace").rstrip("\r") for p in parts]
                if lines:
                    hub.feed(lines, history=catching_up, size=pos - len(buf), src=self)
            if catching_up and pos >= size:
                catching_up = False
                hub.history_done(src=self)
            if not data or not catching_up:
                wait(POLL_SECONDS)


class Importer(threading.Thread):
    """Imports the logs Star Citizen keeps in logbackups, so no session is missing."""
    daemon = True

    def __init__(self, folder, hub):
        super().__init__(name="importer")
        self.folder = Path(folder)
        self.hub = hub
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        time.sleep(0.3)                      # let the live tailer identify the current session first
        try:
            files = sorted(self.folder.glob("*.log"), key=lambda p: p.stat().st_mtime)
        except OSError:
            files = []
        store, total, done, imported = self.hub.store, len(files), 0, 0
        self.hub.set_import(src=self, running=bool(files), done=0, total=total, imported=0)
        for f in files:
            if self.stop_event.is_set():
                return
            done += 1
            try:
                size = f.stat().st_size
            except OSError:
                continue
            line = first_line_of(f)
            if not line:
                continue
            sid = session_key(line)
            if sid == self.hub.sid:
                continue
            info = store.session(sid)
            if info and info["parser"] == PARSER_VERSION and info["size"] == size:
                continue
            self.import_file(f, sid, size)
            imported += 1
            self.hub.set_import(src=self, running=True, done=done, total=total, imported=imported)
            if imported % 15 == 0:
                self.hub.sessions_changed()
        self.hub.set_import(src=self, running=False, done=done, total=total, imported=imported)
        if imported:
            self.hub.sessions_changed()
            con(f"History: imported {imported} session(s) from {self.folder}", "90")

    def import_file(self, path, sid, size):
        store = self.hub.store
        store.begin_session(sid, path)
        parser, batch = Parser(), []
        with open(path, "rb") as fh:
            for raw in fh:
                if self.stop_event.is_set():
                    return
                batch.extend(parser.feed(raw.decode("utf-8", "replace").rstrip("\r\n")))
                if len(batch) >= 2000:
                    store.add_events(sid, batch)
                    batch = []
        store.add_events(sid, batch)
        store.update_session(sid, parser.state, size)


class ReplaySource(threading.Thread):
    daemon = True

    def __init__(self, path, hub, speed):
        super().__init__(name="replay")
        self.path = Path(path)
        self.hub = hub
        self.speed = max(speed, 0.1)

    def run(self):
        line = first_line_of(self.path) or str(self.path).encode()
        self.hub.begin_session(session_key(line), self.path)
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
        con("Replay finished.", "92")


# ---------------------------------------------------------------------------
# Settings and file dialog
# ---------------------------------------------------------------------------

def load_config():
    for path in (config_path(), app_dir() / "sc_log_tracker.json"):   # second one: v1.0 location
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


def save_config(cfg):
    path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


def resolve_user_path(value):
    """A Game.log path from a file path, a LIVE folder or a StarCitizen folder. None if it can't be one."""
    if not value:
        return None
    p = Path(str(value).strip().strip('"'))
    if p.is_file():
        return p
    if p.is_dir():
        logs = logs_in_folder(p)
        if logs:
            return logs[0]
        for ch in CHANNELS:
            if (p / ch).is_dir():
                return p / ch / "Game.log"
        if (p / "Bin64").is_dir() or (p / "logbackups").is_dir() or (p / "Data.p4k").exists():
            return p / "Game.log"
        return None
    if p.name.lower() == "game.log" and p.parent.is_dir():
        return p                             # the game hasn't created it yet
    return None


_dialog_lock = threading.Lock()


def browse_for_log(initial=None):
    """Native file dialog (tkinter). Returns (path or None, error or None)."""
    with _dialog_lock:
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
        except Exception:  # noqa: BLE001 - no tkinter or no display
            return None, "The file dialog isn't available here. Paste the path instead."
        try:
            root.withdraw()
            root.attributes("-topmost", True)
            root.update()
            start = Path(initial).parent if initial else None
            path = filedialog.askopenfilename(
                parent=root, title="Select your Star Citizen Game.log",
                initialdir=str(start) if start and start.is_dir() else None,
                filetypes=[("Star Citizen log", "Game.log"), ("Log files", "*.log"), ("All files", "*.*")])
        finally:
            root.destroy()
        return (str(Path(path)) if path else None), None


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = APP_NAME


def remove_old_autostart():
    """Versions before 1.4 could start with Windows. That option is gone: remove its entry."""
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_NAME)
        return True
    except OSError:
        return False


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
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _host_ok(self):
        """Only answer requests addressed to this machine (blocks DNS-rebinding tricks)."""
        port = self.server.server_address[1]
        return (self.headers.get("Host") or "").lower() in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "text/plain", b"forbidden")
        url = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(url.query)
        hub = self.hub
        if url.path in ("/", "/index.html"):
            page = PAGE.replace("{{VERSION}}", __version__).replace("{{REPO}}", REPO_URL)
            return self._send(200, "text/html; charset=utf-8", page.encode("utf-8"))
        if url.path == "/stream":
            return self._stream()
        if url.path == "/api/ping":
            return self._json({"app": APP_NAME, "version": __version__})
        if url.path == "/api/state":
            with hub.lock:
                return self._json({"state": dict(hub.parser.state), "meta": dict(hub.meta)})
        if url.path == "/api/events":
            frm = (qs.get("from") or [None])[0] or None
            to = (qs.get("to") or [None])[0] or None
            try:
                limit = max(1, min(int((qs.get("limit") or [EVENTS_LIMIT])[0]), 100000))
            except ValueError:
                limit = EVENTS_LIMIT
            events, total = hub.store.events(frm, to, limit)
            return self._json({"events": events, "total": total, "limit": limit})
        if url.path == "/api/servers":
            frm = (qs.get("from") or [None])[0] or None
            to = (qs.get("to") or [None])[0] or None
            visits = hub.store.server_visits(frm, to)
            servers, regions = {}, {}
            for v in visits:
                v["live"] = bool(v["open"] and v["sid"] == hub.sid and hub.game_running())
                s = servers.setdefault(v["shard"], {"shard": v["shard"], "region": v["region"], "build": v["build"],
                                                    "number": v["number"], "visits": 0, "seconds": 0,
                                                    "first": v["start"], "last": v["end"], "live": False})
                s["visits"] += 1
                s["seconds"] += v["seconds"]
                s["first"], s["last"] = min(s["first"], v["start"]), max(s["last"], v["end"])
                s["live"] = s["live"] or v["live"]
                r = regions.setdefault(v["region"], {"region": v["region"], "visits": 0, "seconds": 0})
                r["visits"] += 1
                r["seconds"] += v["seconds"]
            return self._json({"visits": visits,
                               "servers": sorted(servers.values(), key=lambda s: s["last"], reverse=True),
                               "regions": sorted(regions.values(), key=lambda r: r["seconds"], reverse=True)})
        if url.path == "/api/sessions":
            rows, running = hub.store.sessions(), hub.game_running()
            for r in rows:
                r["live"] = running and r["id"] == hub.sid
            return self._json({"sessions": rows})
        if url.path == "/api/recap":
            frm = (qs.get("from") or [None])[0] or None
            to = (qs.get("to") or [None])[0] or None
            recaps, places = hub.store.recap(frm, to)
            running = hub.game_running()
            for r in recaps:
                r["live"] = running and r["sid"] == hub.sid
            return self._json({"sessions": recaps, "places": places})
        if url.path == "/api/config":
            return self._json(self._config())
        self._send(404, "text/plain", b"not found")

    def do_POST(self):
        # The custom header can't be sent cross-site without a CORS preflight, which we never allow.
        if not self._host_ok() or self.headers.get(CSRF_HEADER) != "1":
            return self._send(403, "text/plain", b"forbidden")
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 65536)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        path, hub = urllib.parse.urlsplit(self.path).path, self.hub
        if path == "/api/config":
            if hub.mode != "live":
                return self._json({"error": "Not available in replay mode."}, 400)
            log = resolve_user_path(body.get("log"))
            if not log:
                return self._json({"error": "Couldn't find a Game.log there. Pick the Game.log file, "
                                            "or your StarCitizen or LIVE folder."}, 400)
            hub.switch_log(log)
            return self._json({"ok": True, "log": str(log)})
        if path == "/api/browse":
            chosen, err = browse_for_log(hub.meta.get("path"))
            if err:
                return self._json({"error": err}, 501)
            return self._json({"path": chosen} if chosen else {"cancelled": True})
        if path == "/api/quit":
            self._json({"ok": True})
            con(str(body.get("reason") or "Quit from the dashboard.")[:200], "90")
            threading.Timer(0.3, lambda: exit_app()).start()
            return
        self._send(404, "text/plain", b"not found")

    def _config(self):
        hub = self.hub
        found = find_game_logs()
        return {"log": hub.meta.get("path"), "mode": hub.mode,
                "default_log": str(found[0]) if found else DEFAULT_LOG,
                "data_dir": str(DATA_DIR), "history": hub.store.stats(), "version": __version__}

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q, snap = self.hub.subscribe()
        last_write = time.time()
        try:
            self._sse(snap)
            while not getattr(q, "dead", False):
                try:
                    msg = q.get(timeout=1)
                except queue.Empty:
                    if self._client_gone():
                        break
                    if time.time() - last_write >= 15:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_write = time.time()
                    continue
                self._sse(msg)
                last_write = time.time()
        except OSError:
            pass
        finally:
            self.hub.unsubscribe(q)

    def _client_gone(self):
        """True once the browser closed the connection (a browser never sends anything on it)."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _sse(self, msg):
        self.wfile.write(b"data: " + json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n\n")
        self.wfile.flush()


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second program bind a port that is already in use,
    # so the "next free port" fallback would never kick in. Other systems need it for
    # quick restarts.
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address):
        # A browser closing or reloading a tab cuts its connections. That's normal, not an error.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def start_server(hub, port):
    Handler.hub = hub
    last_err = None
    for p in range(port, port + PORT_RANGE):
        try:
            srv = Server(("127.0.0.1", p), Handler)
            srv.daemon_threads = True
            return srv, p
        except OSError as e:
            last_err = e
    raise SystemExit(f"No free port found from {port} upwards: {last_err}")


LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never via a proxy


def version_tuple(v):
    """'1.10.2' -> (1, 10, 2), so versions compare correctly. Unknown parts count as 0."""
    parts = []
    for piece in str(v or "").split(".")[:4]:
        m = re.match(r"\d+", piece.strip())
        parts.append(int(m.group()) if m else 0)
    return tuple(parts) or (0,)


def ping(port, timeout=0.5):
    """The /api/ping answer of an SC Log Tracker on this port, or None."""
    try:
        with LOCAL.open(f"http://127.0.0.1:{port}/api/ping", timeout=timeout) as r:
            data = json.loads(r.read())
    except Exception:  # noqa: BLE001 - nothing there, or something else
        return None
    return data if isinstance(data, dict) and data.get("app") == APP_NAME else None


def find_running_instance(port):
    """(port, version) of an SC Log Tracker that is already running on this PC, or None."""
    for p in range(port, port + PORT_RANGE):
        info = ping(p)
        if info:
            return p, str(info.get("version") or "0")
    return None


def stop_instance(port, timeout=10):
    """Ask the tracker on this port to quit and wait until it's gone. True if it stopped."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/quit", method="POST",
        data=json.dumps({"reason": f"Replaced by version {__version__}."}).encode(),
        headers={"Content-Type": "application/json", CSRF_HEADER: "1"})
    try:
        with LOCAL.open(req, timeout=3) as r:
            r.read()
    except Exception:  # noqa: BLE001 - an old version that can't quit, or already gone
        pass
    end = time.time() + timeout
    while time.time() < end:
        if not ping(port, timeout=0.3):
            time.sleep(0.3)              # let the old process release the database and port
            return True
        time.sleep(0.2)
    return False


def take_over_or_open(port, open_browser, version=None):
    """Handle an instance that is already running. True if this process should just exit.

    An older version is stopped so this one can take over (e.g. after an update while the
    old one still runs in the background). The same or a newer version is simply opened.
    """
    version = version or __version__
    running = find_running_instance(port)
    if not running:
        return False
    rport, rversion = running
    if version_tuple(rversion) < version_tuple(version):
        con(f"Version {rversion} is running on port {rport}, replacing it with {version} ...")
        if stop_instance(rport):
            return False
        con("The old version didn't quit, opening it instead.", "93")
    url = f"http://127.0.0.1:{rport}/"
    con(f"Already running, opening {url}")
    if open_browser:
        webbrowser.open(url)
    return True


def exit_app():
    """End the whole process right away, including the threads that are still working."""
    os._exit(0)


class AutoQuit(threading.Thread):
    """Quits the app once the dashboard has been closed. The game's logs are imported on the next start."""
    daemon = True

    def __init__(self, hub, grace=QUIT_GRACE):
        super().__init__(name="autoquit")
        self.hub = hub
        self.grace = grace

    def run(self):
        while True:
            time.sleep(0.5)
            closed = self.hub.dashboard_closed_for()
            if closed is not None and closed >= self.grace:
                con("Dashboard closed, quitting.", "90")
                exit_app()
                return


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------

CAT_LABEL = {"session": "Session", "travel": "Travel", "ship": "Ship", "mission": "Mission", "activity": "Activity",
             "economy": "Trade", "combat": "Combat", "law": "Law", "social": "Party",
             "notice": "Notice", "error": "Error"}
CAT_ANSI = {"session": "90", "travel": "96", "ship": "94", "mission": "93", "activity": "36", "economy": "92",
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


NO_CONSOLE = False      # started without a console (the .exe or pythonw)


def setup_output():
    """Without a console, write messages to tracker.log in the data folder instead."""
    global NO_CONSOLE
    if sys.stdout is not None and sys.stderr is not None:
        return
    NO_CONSOLE = True
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        log = DATA_DIR / "tracker.log"
        if log.exists() and log.stat().st_size > 1_000_000:
            os.replace(log, DATA_DIR / "tracker.old.log")
        f = open(log, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives as long as the app
    except OSError:
        f = open(os.devnull, "w")  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


def show_error(message):
    con(message, "91")
    if os.name == "nt" and (NO_CONSOLE or getattr(sys, "frozen", False)):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, str(message), APP_NAME, 0x10)
        except Exception:  # noqa: BLE001
            pass


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


def resolve_log(arg):
    """Game.log from the command line, the saved settings or auto-detection. None if not found."""
    if arg:
        return resolve_user_path(arg) or Path(arg.strip().strip('"'))
    cfg = load_config()
    if cfg.get("log") and Path(cfg["log"]).parent.is_dir():
        return Path(cfg["log"])
    logs = find_game_logs()
    return logs[0] if logs else None


def resolve_replay(arg, live_path):
    if arg.lower() in ("last", "latest"):
        if not live_path:
            raise SystemExit("Couldn't find your Game.log, so there is no logbackups folder to replay from.")
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
        description="Real-time viewer and history for the Star Citizen Game.log.",
        epilog=REPO_URL)
    ap.add_argument("log", nargs="?", help="Game.log, LIVE folder or StarCitizen folder")
    # Used by the "Start with Windows" entry of versions before 1.4: now it only removes that entry.
    ap.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--replay", metavar="FILE",
                    help="replay a log file, not saved to history ('last' = newest file in logbackups)")
    ap.add_argument("--speed", type=float, default=30.0, help="replay speed (default 30x)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"web port (default {DEFAULT_PORT})")
    ap.add_argument("--data-dir", metavar="DIR", help=f"where settings and history are stored (default {DATA_DIR})")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    ap.add_argument("--quiet", action="store_true", help="don't print events to the console")
    ap.add_argument("--no-color", action="store_true", help="plain console output")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return ap.parse_args(argv)


def run(args):
    global USE_COLOR, DATA_DIR
    if args.data_dir:
        DATA_DIR = Path(args.data_dir).expanduser()
    interactive = bool(getattr(sys.stdout, "isatty", lambda: False)())
    if os.name == "nt" and interactive:
        os.system("")                    # enable ANSI colours in the Windows console
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass
    USE_COLOR = interactive and not args.no_color
    quiet = args.quiet
    open_browser = not args.no_browser

    if args.background:                  # started by an old "Start with Windows" entry
        remove_old_autostart()
        return

    con(f"{APP_NAME} {__version__}", "96")

    if not args.replay and take_over_or_open(args.port, open_browser):
        return

    replay_last = bool(args.replay) and args.replay.lower() in ("last", "latest")
    live_path = None if args.replay and not replay_last else resolve_log(args.log)

    if args.replay:
        src_path = resolve_replay(args.replay, live_path)
        hub = Hub(Store(":memory:"), "replay", quiet)
        hub.meta["path"] = str(src_path)
        source = ReplaySource(src_path, hub, args.speed)
        con(f"Replay:   {src_path}  ({args.speed:g}x speed)")
    else:
        hub = Hub(Store(DATA_DIR / "history.db"), "live", quiet)
        con(f"History:  {DATA_DIR / 'history.db'}")

    srv, port = start_server(hub, args.port)
    url = f"http://127.0.0.1:{port}/"
    threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()
    if args.replay:
        source.start()
    elif live_path:
        hub.switch_log(live_path, remember=bool(args.log) or not load_config().get("log"))
        if not live_path.exists():
            con("The Game.log doesn't exist yet. Waiting for Star Citizen to start ...", "93")
    else:
        hub.set_meta(needs_setup=True)
        con("Couldn't find your Game.log. Choose it in the dashboard.", "93")
    if not args.replay and remove_old_autostart():
        con("Removed the old \"Start with Windows\" entry.", "90")
    AutoQuit(hub).start()                # close the dashboard and the app goes too
    con(f"Viewer:   {url}", "92")
    con("Closing the dashboard stops the tracker (or press Ctrl+C).\n", "90")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        con("\nStopped.", "90")
        srv.shutdown()


def main(argv=None):
    setup_output()
    args = parse_args(argv)
    try:
        run(args)
    except SystemExit as e:
        if e.code not in (None, 0):
            show_error(e.code if isinstance(e.code, str) else f"Exited with code {e.code}")
        raise
    except Exception as e:
        show_error(f"{APP_NAME} stopped because of an error:\n{type(e).__name__}: {e}")
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
  --c-combat:#ff6464;--c-law:#ff9a3d;--c-social:#c792ea;--c-notice:#9aa8bd;--c-error:#ff3d63;--c-activity:#f78fb3;
  --s-stanton:#3cc8f2;--s-pyro:#ff9a3d;--s-nyx:#a98bff;
  --mono:"Cascadia Mono","Consolas","SFMono-Regular",monospace;
  color-scheme:dark;
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
.path{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--faint);overflow:hidden;white-space:nowrap;
  text-overflow:ellipsis;min-width:0;flex:0 1 auto;background:none;border:0;cursor:pointer;padding:0}
.status,.brand,header .btn{flex-shrink:0}
.path:hover{color:var(--muted)}
.btn{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:5px 11px;cursor:pointer;
  font-size:12px;white-space:nowrap}
.btn:hover{border-color:var(--accent)}
.btn.on{border-color:var(--accent);color:var(--accent)}
.btn.primary{background:#0f2a38;border-color:#1f6680;color:#bfeeff}
.btn.danger{border-color:#5a2230;color:#ff9aa8}
.btn.danger.armed{background:#3a1018;border-color:var(--c-error);color:#fff}
.btn:disabled{opacity:.5;cursor:default}
.banner{display:none;align-items:center;gap:12px;padding:7px 16px;font-size:12.5px;border-bottom:1px solid var(--line);background:#0a1626;color:var(--muted)}
.banner.show{display:flex}
.bar{flex:0 0 160px;height:5px;border-radius:3px;background:var(--line);overflow:hidden}
.bar i{display:block;height:100%;background:var(--accent);width:0;transition:width .3s}
main{flex:1;display:grid;grid-template-columns:320px 1fr;min-height:0}
aside{border-right:1px solid var(--line);overflow:auto;padding:14px;display:flex;flex-direction:column;gap:18px}
h2{margin:0 0 8px;font-size:11px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);
  display:flex;justify-content:space-between;align-items:baseline}
h2 small{font-size:11px;letter-spacing:0;text-transform:none;font-weight:400;color:var(--faint)}
.box{background:var(--panel);border:1px solid var(--line);border-radius:8px}
.range{padding:10px 12px;display:flex;flex-direction:column;gap:10px}
.presets{display:flex;flex-wrap:wrap;gap:5px}
.preset{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:3px 9px;font-size:12px;cursor:pointer;color:var(--muted)}
.preset:hover{border-color:var(--accent)}
.preset.on{border-color:var(--accent);color:var(--accent);background:#0c2030}
.fld{display:grid;grid-template-columns:38px 1fr;align-items:center;gap:8px;font-size:12px;color:var(--muted)}
.fld input{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12.5px;width:100%;outline:0;min-width:0}
.fld input:focus{border-color:var(--accent)}
.hint{font-size:11.5px;color:var(--faint)}
.sessions{max-height:340px;overflow:auto}
.ses{display:flex;justify-content:space-between;gap:10px;padding:7px 12px;border-bottom:1px solid var(--line2);cursor:pointer}
.ses:last-child{border-bottom:0}
.ses:hover{background:#0a1220}
.ses.on{background:#0c2030;box-shadow:inset 3px 0 0 var(--accent)}
.ses .a{font-size:12.5px;font-weight:600}
.ses .b{font-size:11.5px;color:var(--faint)}
.ses .c{font-size:11.5px;color:var(--muted);text-align:right;white-space:nowrap}
.badge{font-size:10px;font-weight:700;letter-spacing:.08em;color:#06120c;background:var(--good);border-radius:4px;padding:0 5px;margin-left:6px}
.note{font-size:11.5px;color:var(--faint);line-height:1.5;margin:0}
.foot{font-size:11px;color:var(--faint);margin-top:auto}
section.feed{display:flex;flex-direction:column;min-height:0;min-width:0}
.tabs{display:flex;gap:2px;padding:0 12px;border-bottom:1px solid var(--line);align-items:center}
.tab{background:none;border:0;border-bottom:2px solid transparent;padding:10px 12px;cursor:pointer;color:var(--muted);font-weight:600}
.tab.on{color:var(--text);border-bottom-color:var(--accent)}
.tab .n{font-weight:400;color:var(--faint);margin-left:4px;font-variant-numeric:tabular-nums}
.rangelabel{margin-left:auto;font-size:12px;color:var(--faint);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.toolbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:9px 12px;border-bottom:1px solid var(--line)}
.chip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--panel);
  color:var(--faint);border-radius:999px;padding:3px 9px;font-size:12px;cursor:pointer;user-select:none}
.chip .sw{width:8px;height:8px;border-radius:50%;background:var(--cc);opacity:.35}
.chip.on{color:var(--text);border-color:color-mix(in srgb,var(--cc) 55%,var(--line))}
.chip.on .sw{opacity:1}
.chip .cnt{color:var(--muted);font-variant-numeric:tabular-nums}
.search{margin-left:12px;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:5px 10px;width:220px;min-width:140px;flex:0 1 220px;outline:0}
.search:focus{border-color:var(--accent)}
.list{flex:1;overflow:auto;min-height:0}
.day{position:sticky;top:0;z-index:1;padding:6px 14px;font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);background:#08101b;border-bottom:1px solid var(--line)}
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
.more{padding:14px;text-align:center;font-size:12px;color:var(--faint)}
.srvsum{display:flex;flex-wrap:wrap;gap:18px 28px;align-items:center;padding:14px;border-bottom:1px solid var(--line)}
.srvsum .st b{display:block;font-size:20px;font-weight:650;font-variant-numeric:tabular-nums;line-height:1.15}
.srvsum .st span{font-size:11px;color:var(--muted)}
.regions{flex:1;min-width:260px;display:flex;flex-direction:column;gap:7px}
.rbar{display:flex;height:8px;border-radius:4px;overflow:hidden;background:var(--line)}
.rbar i{display:block;height:100%;background:var(--rc)}
.rleg{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:var(--muted)}
.rleg span::before{content:"";display:inline-block;width:8px;height:8px;border-radius:2px;background:var(--rc);margin-right:6px}
.rleg b{color:var(--text);font-weight:600}
.vis{display:grid;grid-template-columns:104px 92px minmax(0,1fr) 84px;gap:12px;align-items:baseline;padding:8px 14px;border-bottom:1px solid var(--line2);cursor:pointer}
.vis:hover{background:#0a1220}
.vis .t{font-family:var(--mono);font-size:12px;color:var(--muted)}
.rg{font-size:11px;font-weight:600;color:var(--rc);border:1px solid color-mix(in srgb,var(--rc) 45%,transparent);border-radius:999px;padding:0 8px;justify-self:start;white-space:nowrap}
.sh{font-family:var(--mono);font-size:12.5px;color:var(--text)}
.vis .d{color:var(--faint);font-size:12px;margin-left:8px}
.vis .du{text-align:right;font-variant-numeric:tabular-nums;color:var(--muted);font-size:12.5px}
.srvtab{width:100%;border-collapse:collapse;font-size:12.5px}
.srvtab th{position:sticky;top:0;background:#08101b;text-align:left;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:600;padding:7px 14px;border-bottom:1px solid var(--line)}
.srvtab td{padding:7px 14px;border-bottom:1px solid var(--line2);color:var(--muted)}
.srvtab td.num,.srvtab th.num{text-align:right;font-variant-numeric:tabular-nums}
.srvtab tr:hover td{background:#0a1220}
.rc{border-bottom:1px solid var(--line)}
.rch{padding:12px 14px;cursor:pointer;display:flex;flex-direction:column;gap:8px}
.rch:hover{background:#0a1220}
.rct{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.rct b{font-size:14px;font-weight:650}
.rct .t{font-family:var(--mono);font-size:12px;color:var(--muted)}
.rct .du{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums;font-size:12.5px}
.chev{display:inline-block;color:var(--faint);transition:transform .15s;width:10px;font-size:11px}
.rc.open .chev{transform:rotate(90deg)}
.pills{display:flex;flex-wrap:wrap;gap:6px}
.pl{font-size:12px;color:var(--text);border:1px solid color-mix(in srgb,var(--cc) 40%,var(--line));border-radius:999px;padding:1px 9px;background:color-mix(in srgb,var(--cc) 8%,transparent)}
.pbar{display:flex;height:6px;border-radius:3px;overflow:hidden;background:var(--line);gap:2px}
.pbar i{display:block;height:100%;background:var(--sc);min-width:3px}
.route{font-size:12.5px;color:var(--muted);line-height:1.6}
.route .arr{color:var(--faint);margin:0 2px}
.rc.open .route{display:none}
.rcb{padding:0 14px 14px 20px}
.chp{display:grid;grid-template-columns:44px minmax(0,1fr) 64px;gap:12px;padding:8px 10px;border-left:2px solid var(--sc);cursor:pointer}
.chp:hover{background:#0a1220}
.chp .t{font-family:var(--mono);font-size:12px;color:var(--muted);padding-top:1px}
.cht{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.cht b{font-weight:650}
.sys{font-size:11px;font-weight:600;color:var(--sc);border:1px solid color-mix(in srgb,var(--sc) 45%,transparent);border-radius:999px;padding:0 8px;white-space:nowrap}
.via{font-size:11.5px;color:var(--faint)}
.chp ul{margin:5px 0 0;padding:0;list-style:none;display:flex;flex-direction:column;gap:3px}
.chp li{font-size:12.5px;color:var(--muted);padding-left:13px;position:relative;overflow-wrap:anywhere}
.chp li::before{content:"";position:absolute;left:0;top:.5em;width:6px;height:6px;border-radius:50%;background:var(--cc)}
.chp .du{text-align:right;font-size:12px;color:var(--faint);font-variant-numeric:tabular-nums}
.rcf{padding:10px 0 0 12px}
.plc td{vertical-align:baseline}
.plc .pn{color:var(--text);font-weight:600}
.plc td:last-child,.plc td.num{white-space:nowrap}
.plc tbody tr{cursor:pointer}
.rawlist{font-family:var(--mono);font-size:12px;line-height:1.5;padding:4px 0}
.rl{display:grid;grid-template-columns:64px minmax(0,1fr);padding:0 14px 0 11px;border-left:3px solid transparent;color:#8193ad}
.rl .tx{white-space:pre-wrap;word-break:break-all}
.rl .ln{color:var(--faint);user-select:none}
.rl.hit{border-left-color:var(--cc);color:var(--text);background:#0a1220}
.rl.e{color:#ff8fa3} .rl.w{color:#e8c47e}
mark{background:rgba(60,200,242,.28);color:inherit;border-radius:2px}
.empty-msg{padding:40px 20px;text-align:center;color:var(--faint)}
.toast{position:fixed;right:18px;bottom:18px;background:var(--panel2);border:1px solid var(--accent);border-radius:8px;
  padding:10px 14px;font-size:13px;opacity:0;transform:translateY(8px);transition:.25s;pointer-events:none;z-index:30}
.toast.show{opacity:1;transform:none}
.modal{position:fixed;inset:0;background:rgba(2,5,10,.72);display:none;align-items:flex-start;justify-content:center;z-index:20;overflow:auto;padding:6vh 16px}
.modal.show{display:flex}
.dialog{width:min(640px,100%);background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.dialog header{border-radius:12px 12px 0 0;justify-content:space-between}
.dialog h3{margin:0;font-size:15px}
.dsec{padding:16px 18px;border-bottom:1px solid var(--line2);display:flex;flex-direction:column;gap:10px}
.dsec:last-child{border-bottom:0}
.dsec h4{margin:0;font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.dsec p{margin:0;font-size:13px;color:var(--muted)}
.row{display:flex;gap:8px;align-items:center}
.row input[type=text]{flex:1;min-width:0;background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:7px 10px;font-family:var(--mono);font-size:12px;outline:0}
.row input[type=text]:focus{border-color:var(--accent)}
.defpath{font-family:var(--mono);font-size:11.5px;color:var(--faint);overflow-wrap:anywhere;margin-top:-4px;cursor:pointer;background:none;border:0;padding:0;text-align:left}
.defpath:hover{color:var(--muted)}
.msg{font-size:12.5px;min-height:1em}
.msg.err{color:var(--bad)} .msg.ok{color:var(--good)}
.stopped{position:fixed;inset:0;background:var(--bg);display:none;align-items:center;justify-content:center;flex-direction:column;gap:8px;z-index:40;color:var(--muted)}
.stopped.show{display:flex}
@media (max-width:860px){
  main{grid-template-columns:1fr;grid-template-rows:auto 1fr}
  aside{border-right:0;border-bottom:1px solid var(--line);max-height:46vh}
  .path,.who,.rangelabel{display:none}
  header{flex-wrap:wrap;gap:8px 12px} .status{flex-shrink:1;white-space:normal} header .btn:first-of-type{margin-left:auto}
  .tabs{flex-wrap:wrap;padding-bottom:8px} .search{flex:1 1 100%;margin-left:0}
  .ev{grid-template-columns:62px minmax(0,1fr)} .ev .b{display:none} .ev .raw{grid-column:1/-1}
  .chp{grid-template-columns:40px minmax(0,1fr)} .chp .du{display:none} .rcb{padding-left:10px}
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
  <button class="path" id="hPath" title="Change the Game.log location"></button>
  <button class="btn" id="btnExport" title="Save the events of the selected time range as JSON">Export</button>
  <button class="btn" id="btnSettings">Settings</button>
</header>
<div class="banner" id="banner"><span id="bannerText"></span><span class="bar" id="bannerBar"><i></i></span></div>
<main>
  <aside>
    <div>
      <h2>Time range</h2>
      <div class="box range">
        <div class="presets" id="presets">
          <button class="preset" data-p="session">This session</button>
          <button class="preset" data-p="today">Today</button>
          <button class="preset" data-p="24h">24 hours</button>
          <button class="preset" data-p="7d">7 days</button>
          <button class="preset" data-p="30d">30 days</button>
          <button class="preset" data-p="all">All</button>
        </div>
        <label class="fld">From <input type="datetime-local" id="rFrom" step="60"></label>
        <label class="fld">To <input type="datetime-local" id="rTo" step="60"></label>
        <div class="hint" id="rHint">Leave "To" empty to keep following live.</div>
      </div>
    </div>
    <div>
      <h2>History <small id="sesCount"></small></h2>
      <div class="box sessions" id="sessions"><div class="empty-msg">No sessions yet.</div></div>
    </div>
    <p class="note">Since the 2026 builds, kills, K/D and your aUEC balance are no longer written to the Game.log. Deaths only show up in some cases, for example when your ship is destroyed.</p>
    <p class="foot">SC Log Tracker {{VERSION}} · <a href="{{REPO}}" target="_blank" rel="noopener">GitHub</a></p>
  </aside>
  <section class="feed">
    <div class="tabs">
      <button class="tab on" data-tab="ev">Events<span class="n" id="cEv">0</span></button>
      <button class="tab" data-tab="ses" title="Where you were and what you did there">Sessions<span class="n" id="cSes">0</span></button>
      <button class="tab" data-tab="srv" title="Which servers (shards) you were on">Servers<span class="n" id="cSrv">0</span></button>
      <button class="tab" data-tab="raw" title="Raw lines of the current game session">Raw log<span class="n" id="cRaw">0</span></button>
      <span class="rangelabel" id="rangeLabel"></span>
      <input class="search" id="search" type="search" placeholder="Search ..." autocomplete="off">
    </div>
    <div class="toolbar">
      <span id="chips" style="display:contents"></span>
      <button class="btn on" id="btnRecap" style="display:none" title="One card per session: your stops and what you did there">Sessions</button>
      <button class="btn" id="btnPlaces" style="display:none" title="Every place you were: visits, time and what you did there">Places</button>
      <button class="btn on" id="btnVisits" style="display:none" title="Every time you joined a server">Visits</button>
      <button class="btn" id="btnByServer" style="display:none" title="One row per server">By server</button>
      <button class="btn" id="btnHits" style="display:none" title="Only show lines that produced an event">Matches only</button>
      <button class="btn on" id="btnScroll" style="display:none">Auto-scroll</button>
    </div>
    <div class="list" id="evList"></div>
    <div class="list" id="sesList" style="display:none"></div>
    <div class="list" id="srvList" style="display:none"></div>
    <div class="list rawlist" id="rawList" style="display:none"></div>
  </section>
</main>

<div class="modal" id="settings" role="dialog" aria-modal="true" aria-labelledby="setTitle">
  <div class="dialog">
    <header><h3 id="setTitle">Settings</h3><button class="btn" id="setClose">Close</button></header>
    <div class="dsec">
      <h4>Game.log</h4>
      <p id="setIntro">Where Star Citizen writes its log. Pick the Game.log file, or your StarCitizen or LIVE folder.</p>
      <div class="row"><input type="text" id="setPath" placeholder="C:\Program Files\Roberts Space Industries\StarCitizen\LIVE\Game.log" spellcheck="false">
        <button class="btn" id="setBrowse">Browse ...</button><button class="btn primary" id="setSave">Use this</button></div>
      <button class="defpath" id="setDefault" title="Use this path"></button>
      <div class="msg" id="setMsg"></div>
    </div>
    <div class="dsec">
      <h4>History</h4>
      <p id="setHistory">–</p>
      <p class="hint">Missed sessions are filled in from Star Citizen's own logbackups folder every time the tracker starts, so it doesn't need to run while you play.</p>
    </div>
    <div class="dsec">
      <h4>Tracker</h4>
      <div class="row"><button class="btn danger" id="btnQuit">Quit SC Log Tracker</button><span class="hint">Stops the tracker. Closing the dashboard does the same after a few seconds.</span></div>
    </div>
  </div>
</div>
<div class="stopped" id="stopped"><b>SC Log Tracker has stopped.</b><span>You can close this tab.</span></div>
<div class="toast" id="toast"></div>
<script>
const CATS={session:"Session",travel:"Travel",ship:"Ship",mission:"Mission",activity:"Activity",economy:"Trade",
  combat:"Combat",law:"Law",social:"Party",notice:"Notice",error:"Error"};
const MAX_RAW=4000, PAGE_SIZE=400;
let events=[], total=0, limit=0, raw=[], state={}, meta={}, sessions=[];
let active=new Set(Object.keys(CATS)), query="", tab="ev", onlyHits=false, autoScroll=true;
const TABS=["ev","ses","srv","raw"];
let connected=false, lastLineAt=0, shownList=[], rendered=0, loadSeq=0, lastImportRunning=false, stopped=false;
let range=loadRange();
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num=n=>Number(n||0).toLocaleString("en-US",{maximumFractionDigits:0});
const pad=n=>String(n).padStart(2,"0");
function api(path,body){ const o={headers:{"X-SC-Log-Tracker":"1","Content-Type":"application/json"}};
  if(body!==undefined){ o.method="POST"; o.body=JSON.stringify(body); } return fetch(path,o).then(r=>r.json().catch(()=>({error:"HTTP "+r.status}))); }
function tLocal(ts){ if(!ts) return "--:--:--"; const d=new Date(ts); return isNaN(d)?ts.slice(11,19):d.toLocaleTimeString("en-GB",{hour12:false}); }
function dayKey(ts){ const d=new Date(ts); return d.getFullYear()+"-"+pad(d.getMonth()+1)+"-"+pad(d.getDate()); }
function dayLabel(ts){ const d=new Date(ts), t=new Date(); const k=dayKey(ts);
  if(k===dayKey(t)) return "Today"; const y=new Date(t); y.setDate(t.getDate()-1); if(k===dayKey(y)) return "Yesterday";
  return d.toLocaleDateString("en-GB",{weekday:"short",day:"numeric",month:"short",year:"numeric"}); }
function shortDT(ts){ const d=new Date(ts); return d.toLocaleDateString("en-GB",{day:"numeric",month:"short"})+" "+pad(d.getHours())+":"+pad(d.getMinutes()); }
function toInput(iso){ if(!iso) return ""; const d=new Date(iso); return d.getFullYear()+"-"+pad(d.getMonth()+1)+"-"+pad(d.getDate())+"T"+pad(d.getHours())+":"+pad(d.getMinutes()); }
function fromInput(v){ if(!v) return null; const d=new Date(v); return isNaN(d)?null:d.toISOString(); }
function dur(a,b){ if(!a||!b) return "–"; let s=Math.max(0,(new Date(b)-new Date(a))/1000|0); const h=s/3600|0; const m=(s%3600)/60|0; return h?`${h}h ${pad(m)}m`:`${m}m`; }
function hl(s){ s=esc(s); if(!query) return s; const q=esc(query).replace(/[.*+?^${}()|[\]\\]/g,"\\$&"); return s.replace(new RegExp(q,"gi"),m=>"<mark>"+m+"</mark>"); }
function matches(e){ if(!active.has(e.c)) return false; if(!query) return true;
  const q=query.toLowerCase(); return (e.ti+" "+e.d+" "+CATS[e.c]).toLowerCase().includes(q) || (e.raw||"").toLowerCase().includes(q); }
function loadRange(){ try{ const r=JSON.parse(localStorage.getItem("sclt.range")||"null"); if(r&&r.preset) return r; }catch(e){} return {preset:"24h"}; }
function saveRange(){ try{ localStorage.setItem("sclt.range",JSON.stringify(range)); }catch(e){} }
function toast(msg){ const t=$("toast"); t.textContent=msg; t.classList.add("show"); clearTimeout(t._h); t._h=setTimeout(()=>t.classList.remove("show"),4000); }

/* ---------- time range ---------- */
function bounds(){
  const now=Date.now(), iso=ms=>new Date(ms).toISOString();
  switch(range.preset){
    case "session": return {from:state.first_ts||iso(now), to:null};
    case "today": { const d=new Date(); d.setHours(0,0,0,0); return {from:d.toISOString(), to:null}; }
    case "24h": return {from:iso(now-864e5), to:null};
    case "7d": case "30d": { const d=new Date(); d.setHours(0,0,0,0); d.setDate(d.getDate()-(range.preset==="7d"?6:29)); return {from:d.toISOString(), to:null}; }
    case "all": return {from:null, to:null};
    default: return {from:range.from||null, to:range.to||null};
  }
}
function renderRange(){
  document.querySelectorAll(".preset").forEach(b=>b.classList.toggle("on",b.dataset.p===range.preset));
  const b=bounds();
  if(document.activeElement!==$("rFrom")) $("rFrom").value=toInput(b.from);
  if(document.activeElement!==$("rTo")) $("rTo").value=toInput(b.to);
  const live=!b.to;
  $("rHint").textContent = live ? "Following live. Set \"To\" to look at a fixed period." : "Fixed period. Clear \"To\" to follow live again.";
  const lbl = range.preset==="all" ? "All time" : (b.from?shortDT(b.from):"Start")+" – "+(b.to?shortDT(b.to):"now");
  $("rangeLabel").textContent = lbl + (live?" · live":"");
  document.querySelectorAll(".ses").forEach(el=>el.classList.toggle("on",el.dataset.id===range.sid));
}
function setRange(r){ range=r; saveRange(); recOpen=null; renderRange(); loadEvents(); }
$("presets").addEventListener("click",e=>{ const b=e.target.closest(".preset"); if(b) setRange({preset:b.dataset.p}); });
function customChanged(){ const f=fromInput($("rFrom").value), t=fromInput($("rTo").value); setRange({preset:"custom",from:f,to:t}); }
$("rFrom").addEventListener("change",customChanged); $("rTo").addEventListener("change",customChanged);

function loadEvents(){
  loadServers(); loadRecap();
  const b=bounds(), seq=++loadSeq; const qs=new URLSearchParams();
  if(b.from) qs.set("from",b.from); if(b.to) qs.set("to",b.to);
  api("/api/events?"+qs).then(res=>{ if(seq!==loadSeq||res.error) return;
    events=res.events||[]; total=res.total||0; limit=res.limit||0; renderChips(); renderEvents(); updateCounts(); });
}
function inRange(e){ const b=bounds(); if(b.to) return false; return !b.from || (e.ts && e.ts>=b.from); }

/* ---------- events ---------- */
function evHtml(e,fresh){
  return `<div class="ev ${e.lv}${fresh?" fresh":""}" style="--cc:var(--c-${e.c})">
    <span class="t">${tLocal(e.ts)}</span><span class="b">${CATS[e.c]||e.c}</span>
    <div><span class="ti">${hl(e.ti)}</span>${e.d?`<span class="d">${hl(e.d)}</span>`:""}</div>
    <div class="raw">Line ${e.n}: ${hl(e.raw)}</div></div>`;
}
const dayHtml=ts=>`<div class="day" data-day="${dayKey(ts)}">${dayLabel(ts)}</div>`;
function renderEvents(){
  const list=$("evList"); shownList=events.filter(matches).reverse(); rendered=0; list.innerHTML="";
  if(!shownList.length){ list.innerHTML=`<div class="empty-msg">${events.length?"No events match this filter.":(meta.import&&meta.import.running?"Importing your history ...":"No events in this time range.")}</div>`; return; }
  renderMore(); list.scrollTop=0;
}
function renderMore(){
  const list=$("evList"); const old=list.querySelector(".more"); if(old) old.remove();
  const chunk=shownList.slice(rendered,rendered+PAGE_SIZE);
  let last=rendered?dayKey(shownList[rendered-1].ts):null, html="";
  for(const e of chunk){ const k=dayKey(e.ts); if(k!==last){ html+=dayHtml(e.ts); last=k; } html+=evHtml(e,false); }
  rendered+=chunk.length;
  if(rendered<shownList.length) html+=`<div class="more">Scroll for more ...</div>`;
  else if(total>events.length) html+=`<div class="more">Showing the newest ${num(events.length)} of ${num(total)} events. Narrow the time range to see older ones.</div>`;
  list.insertAdjacentHTML("beforeend",html);
}
$("evList").addEventListener("scroll",()=>{ const l=$("evList"); if(rendered<shownList.length && l.scrollTop+l.clientHeight>l.scrollHeight-700) renderMore(); });
function addEvents(evs){
  const fresh=evs.filter(inRange); if(!fresh.length) return;
  events.push(...fresh); total+=fresh.length;
  const list=$("evList"); const ok=fresh.filter(matches);
  if(ok.length){ const em=list.querySelector(".empty-msg"); if(em) em.remove(); }
  for(const e of ok){
    shownList.unshift(e); rendered++;
    const top=list.firstElementChild, k=dayKey(e.ts);
    if(top && top.classList.contains("day") && top.dataset.day===k) top.insertAdjacentHTML("afterend",evHtml(e,true));
    else list.insertAdjacentHTML("afterbegin",dayHtml(e.ts)+evHtml(e,true));
  }
  renderChips(); updateCounts();
}
$("evList").addEventListener("click",ev=>{ const row=ev.target.closest(".ev"); if(row && !window.getSelection().toString()) row.classList.toggle("open"); });

/* ---------- raw log ---------- */
function rawOk(r){ if(onlyHits && !r.k) return false; return !query || r.s.toLowerCase().includes(query.toLowerCase()); }
function rawHtml(r){ return `<div class="rl${r.k?" hit":""}${r.l?" "+r.l:""}"${r.k?` style="--cc:var(--c-${r.k})"`:""}><span class="ln">${r.n}</span><span class="tx">${hl(r.s)}</span></div>`; }
function renderRaw(){
  const list=$("rawList"); const ok=raw.filter(rawOk);
  list.innerHTML = ok.length ? ok.map(rawHtml).join("") : `<div class="empty-msg">No lines from the current game session yet.</div>`;
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
    `<span class="chip${active.has(k)?" on":""}" data-c="${k}" style="--cc:var(--c-${k})" title="Click: show/hide · Double-click: only this"><span class="sw"></span>${l}<span class="cnt">${num(counts[k]||0)}</span></span>`).join("");
}
let chipTimer=null;
$("chips").addEventListener("click",ev=>{ const c=ev.target.closest(".chip"); if(!c) return;
  clearTimeout(chipTimer); chipTimer=setTimeout(()=>{ const k=c.dataset.c; active.has(k)?active.delete(k):active.add(k); renderChips(); renderEvents(); },200); });
$("chips").addEventListener("dblclick",ev=>{ const c=ev.target.closest(".chip"); if(!c) return; clearTimeout(chipTimer);
  const k=c.dataset.c; active = (active.size===1 && active.has(k)) ? new Set(Object.keys(CATS)) : new Set([k]); renderChips(); renderEvents(); });
function showTab(name){
  tab=TABS.includes(name)?name:"ev"; try{ localStorage.setItem("sclt.tab",tab); }catch(e){}
  document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("on",x.dataset.tab===tab));
  $("evList").style.display=tab==="ev"?"":"none"; $("rawList").style.display=tab==="raw"?"":"none";
  $("srvList").style.display=tab==="srv"?"":"none"; $("sesList").style.display=tab==="ses"?"":"none";
  $("btnHits").style.display=$("btnScroll").style.display=tab==="raw"?"":"none";
  $("btnVisits").style.display=$("btnByServer").style.display=tab==="srv"?"":"none";
  $("btnRecap").style.display=$("btnPlaces").style.display=tab==="ses"?"":"none";
  renderChips(); if(tab==="raw") renderRaw(); if(tab==="srv") renderServers(); if(tab==="ses") renderRecap();
}
document.querySelectorAll(".tab").forEach(b=>b.addEventListener("click",()=>showTab(b.dataset.tab)));
let searchTimer=null;
function setQuery(v){ query=v; renderEvents(); if(tab==="raw") renderRaw(); if(tab==="srv") renderServers(); if(tab==="ses") renderRecap(); }
$("search").addEventListener("input",e=>{ clearTimeout(searchTimer); searchTimer=setTimeout(()=>setQuery(e.target.value.trim()),150); });
$("btnHits").addEventListener("click",()=>{ onlyHits=!onlyHits; $("btnHits").classList.toggle("on",onlyHits); renderRaw(); });
$("btnScroll").addEventListener("click",()=>{ autoScroll=!autoScroll; $("btnScroll").classList.toggle("on",autoScroll); if(autoScroll){ const l=$("rawList"); l.scrollTop=l.scrollHeight; } });
function updateCounts(){ $("cEv").textContent=num(total); $("cRaw").textContent=num(state.lines); }

/* ---------- servers ---------- */
let srv={visits:[],servers:[],regions:[]}, srvView="visits", srvSeq=0;
const REGION_COLOR={"Europe":"var(--c-travel)","US East":"var(--c-mission)","US West":"var(--c-law)","US Central":"var(--c-law)",
  "Australia":"var(--c-economy)","Asia":"var(--c-social)","South America":"var(--c-ship)"};
const rc=r=>REGION_COLOR[r]||"var(--c-notice)";
function hm(sec){ sec=Math.max(0,sec|0); const h=sec/3600|0, m=(sec%3600)/60|0; return h?`${h}h ${pad(m)}m`:`${m}m`; }
function loadServers(){ const b=bounds(), seq=++srvSeq, qs=new URLSearchParams();
  if(b.from) qs.set("from",b.from); if(b.to) qs.set("to",b.to);
  api("/api/servers?"+qs).then(r=>{ if(seq!==srvSeq||r.error) return; srv=r; $("cSrv").textContent=num(r.visits.length); if(tab==="srv") renderServers(); }); }
function liveSecs(v){ return v.live ? Math.max(v.seconds,(Date.now()-new Date(v.start))/1000) : v.seconds; }
function srvMatch(text){ return !query || text.toLowerCase().includes(query.toLowerCase()); }
function renderServers(){
  const list=$("srvList"); const vs=srv.visits.filter(v=>srvMatch(v.shard+" "+v.region));
  if(!srv.visits.length){ list.innerHTML=`<div class="empty-msg">No server visits in this time range.</div>`; return; }
  const total=srv.regions.reduce((a,r)=>a+r.seconds,0)||1;
  let html=`<div class="srvsum"><div class="st"><b>${num(srv.visits.length)}</b><span>visits</span></div>
    <div class="st"><b>${num(srv.servers.length)}</b><span>servers</span></div>
    <div class="st"><b>${hm(srv.visits.reduce((a,v)=>a+liveSecs(v),0))}</b><span>online</span></div>
    <div class="regions"><div class="rbar">${srv.regions.map(r=>`<i style="--rc:${rc(r.region)};width:${(100*r.seconds/total).toFixed(2)}%" title="${esc(r.region)}"></i>`).join("")}</div>
    <div class="rleg">${srv.regions.map(r=>`<span style="--rc:${rc(r.region)}"><b>${esc(r.region)}</b> ${Math.round(100*r.seconds/total)}% · ${hm(r.seconds)} · ${num(r.visits)} visit${r.visits===1?"":"s"}</span>`).join("")}</div></div></div>`;
  if(srvView==="visits"){
    let last=null;
    for(const v of vs){ const k=dayKey(v.start); if(k!==last){ html+=dayHtml(v.start); last=k; }
      const why=v.left?({"Exited to menu":"exited to menu","Disconnected from server":"disconnected","Disconnected for inactivity":"kicked for inactivity","Game closed":"game closed","Crash!":"game crashed","Moved to another server":"moved to another server"}[v.left]||v.left.toLowerCase()):(v.live?"still here":"");
      const extra=[v.rejoins?`rejoined ${v.rejoins}×`:"", why].filter(Boolean).join(" · ");
      html+=`<div class="vis" data-i="${srv.visits.indexOf(v)}" title="Show the events of this visit"><span class="t">${tLocal(v.start).slice(0,5)}–${v.live?"now":tLocal(v.end).slice(0,5)}</span>
        <span class="rg" style="--rc:${rc(v.region)}">${esc(v.region)}</span>
        <div><span class="sh">${hl(v.shard)}</span>${extra?`<span class="d">${esc(extra)}</span>`:""}${v.live?'<span class="badge">LIVE</span>':""}</div>
        <span class="du">${hm(liveSecs(v))}</span></div>`; }
    if(!vs.length) html+=`<div class="empty-msg">No server matches the search.</div>`;
  } else {
    const ss=srv.servers.filter(s=>srvMatch(s.shard+" "+s.region));
    html+=`<table class="srvtab"><thead><tr><th>Server</th><th>Region</th><th class="num">Visits</th><th class="num">Time</th><th>First seen</th><th>Last seen</th></tr></thead><tbody>`+
      ss.map(s=>`<tr><td><span class="sh">${hl(s.shard)}</span>${s.live?'<span class="badge">LIVE</span>':""}</td><td><span class="rg" style="--rc:${rc(s.region)}">${esc(s.region)}</span></td>
        <td class="num">${num(s.visits)}</td><td class="num">${hm(s.seconds)}</td><td>${shortDT(s.first)}</td><td>${s.live?"now":shortDT(s.last)}</td></tr>`).join("")+`</tbody></table>`;
  }
  list.innerHTML=html;
}
$("srvList").addEventListener("click",e=>{ const el=e.target.closest(".vis"); if(!el) return; const v=srv.visits[+el.dataset.i]; if(!v) return;
  setRange({preset:"custom",from:v.start,to:v.live?null:v.end}); showTab("ev"); });
$("btnVisits").addEventListener("click",()=>{ srvView="visits"; $("btnVisits").classList.add("on"); $("btnByServer").classList.remove("on"); renderServers(); });
$("btnByServer").addEventListener("click",()=>{ srvView="servers"; $("btnByServer").classList.add("on"); $("btnVisits").classList.remove("on"); renderServers(); });
setInterval(()=>{ if(tab==="srv" && srv.visits.some(v=>v.live)) renderServers(); },30000);

/* ---------- sessions: where you were and what you did ---------- */
let rec={sessions:[],places:[]}, recView="sessions", recSeq=0, recOpen=null, recTimer=null;
const sc=s=>({Stanton:"var(--s-stanton)",Pyro:"var(--s-pyro)",Nyx:"var(--s-nyx)"}[s]||"var(--faint)");
const plural=(n,w,ws)=>`${num(n)} ${n===1?w:(ws||w+"s")}`;
function loadRecap(){ const b=bounds(), seq=++recSeq, qs=new URLSearchParams();
  if(b.from) qs.set("from",b.from); if(b.to) qs.set("to",b.to);
  api("/api/recap?"+qs).then(r=>{ if(seq!==recSeq||r.error) return; rec=r; $("cSes").textContent=num(r.sessions.length); if(tab==="ses") renderRecap(); }); }
function recSoon(){ clearTimeout(recTimer); recTimer=setTimeout(loadRecap,8000); }
function placeName(ch){ return !ch.place ? "Unknown location" : (/ System$/.test(ch.place) ? "In space" : ch.place); }
function chEnd(ch,r){ return r.live && ch===r.chapters[r.chapters.length-1] ? new Date() : new Date(ch.end); }
function recText(r){ return [r.summary.party.join(" "), ...r.chapters.map(ch=>placeName(ch)+" "+(ch.system||"")+" "+ch.lines.map(l=>l.t).join(" "))].join(" ").toLowerCase(); }
function sumPills(s){ const p=[], regions=[...new Set(s.servers.map(x=>x.region))], c=s.contracts;
  if(s.servers.length) p.push(["session",`${plural(s.servers.length,"server")} · ${regions.join(", ")}`]);
  if(s.flown.length) p.push(["ship","Flew "+s.flown.slice(0,2).join(", ")+(s.flown.length>2?` +${s.flown.length-2}`:"")]);
  else if(s.boarded.length) p.push(["ship","Aboard "+s.boarded[0].replace(/ \(.*\)$/,"")+(s.boarded.length>1?` +${s.boarded.length-1}`:"")]);
  if(s.spent) p.push(["economy",num(s.spent)+" aUEC spent"]);
  const done=[c.completed&&`${c.completed} completed`,c.failed&&`${c.failed} failed`,c.abandoned&&`${c.abandoned} abandoned`].filter(Boolean);
  if(c.accepted||done.length) p.push(["mission",(c.accepted?plural(c.accepted,"contract")+(done.length?": ":" accepted"):"Contracts: ")+done.join(", ")]);
  Object.entries(s.acts).sort((a,b)=>b[1]-a[1]).slice(0,2).forEach(([k,n])=>p.push(["activity",`${n>1?n+"× ":""}${k.toLowerCase()}`]));
  if(s.jumps) p.push(["travel",plural(s.jumps,"quantum jump")]);
  if(s.deaths) p.push(["combat",`died ${s.deaths}×`]); else if(s.downed) p.push(["combat",`downed ${s.downed}×`]);
  if(s.party.length) p.push(["social","with "+s.party.slice(0,3).join(", ")+(s.party.length>3?` +${s.party.length-3}`:"")]);
  if(s.crashes) p.push(["error",s.crashes>1?`crashed ${s.crashes}×`:"crashed"]);
  return p.map(([k,t])=>`<span class="pl" style="--cc:var(--c-${k})">${hl(t)}</span>`).join("");
}
function chHtml(r,ch,i){
  const secs=(chEnd(ch,r)-new Date(ch.start))/1000;
  return `<div class="chp" data-i="${i}" style="--sc:${sc(ch.system)}" title="Show the events of this stop">
    <span class="t">${tLocal(ch.start).slice(0,5)}</span>
    <div><div class="cht"><b>${hl(placeName(ch))}</b>${ch.system?`<span class="sys">${esc(ch.system)}</span>`:""}${ch.via==="quantum"?'<span class="via">by quantum jump</span>':""}</div>
      ${ch.lines.length?`<ul>${ch.lines.map(l=>`<li style="--cc:var(--c-${l.c})">${hl(l.t)}</li>`).join("")}</ul>`:""}</div>
    <span class="du">${secs>=60?"~"+hm(secs):"&lt;1m"}</span></div>`;
}
function recHtml(r,open){
  const secs=r.live?Math.max(r.seconds,(Date.now()-new Date(r.start))/1000):r.seconds;
  const segs=r.chapters.map(ch=>({ch,w:Math.max(0,chEnd(ch,r)-new Date(ch.start))})), tot=segs.reduce((a,x)=>a+x.w,0)||1;
  const bar=segs.length?`<div class="pbar">${segs.map(x=>`<i style="--sc:${sc(x.ch.system)};width:${(100*x.w/tot).toFixed(2)}%" title="${esc(placeName(x.ch))} · ${hm(x.w/1000)}"></i>`).join("")}</div>`:"";
  const route=r.summary.route.map(p=>hl(p)).join('<span class="arr"> → </span>');
  return `<div class="rc${open?" open":""}" data-sid="${r.sid}">
    <div class="rch" title="${open?"Hide":"Show"} the stops of this session"><div class="rct"><span class="chev">▶</span><b>${dayLabel(r.start)}</b>
      <span class="t">${tLocal(r.start).slice(0,5)} – ${r.live?"now":tLocal(r.end).slice(0,5)}</span>${r.live?'<span class="badge">LIVE</span>':""}<span class="du">${hm(secs)}</span></div>
      <div class="pills">${sumPills(r.summary)}</div>${bar}
      <div class="route">${route||"No places recorded in this session."}</div></div>
    ${open?`<div class="rcb">${r.chapters.length?r.chapters.map((ch,i)=>chHtml(r,ch,i)).join(""):'<div class="empty-msg">No places recorded in this session.</div>'}
      <div class="rcf"><button class="btn" data-act="events">Show all events of this session</button></div></div>`:""}</div>`;
}
function renderRecap(){
  if(recView==="places") return renderPlaces();
  const list=$("sesList");
  if(!rec.sessions.length){ list.innerHTML=`<div class="empty-msg">${meta.import&&meta.import.running?"Importing your history ...":"No sessions in this time range."}</div>`; return; }
  if(!recOpen) recOpen=new Set(rec.sessions.length<=3?rec.sessions.map(r=>r.sid):[rec.sessions[0].sid]);
  const q=query.toLowerCase(), rs=rec.sessions.filter(r=>!q||recText(r).includes(q));
  list.innerHTML = rs.length ? rs.map((r,i)=>recHtml(r,recOpen.has(r.sid)||(!!q&&i<5))).join("") : `<div class="empty-msg">No session matches "${esc(query)}".</div>`;
}
function placeDid(p){ const a=Object.entries(p.acts).sort((x,y)=>y[1]-x[1]).map(([k,n])=>`${n>1?n+"× ":""}${k.toLowerCase()}`);
  if(p.contracts) a.push(plural(p.contracts,"contract")+" accepted"); if(p.spent) a.push(num(p.spent)+" aUEC spent"); return a.slice(0,3).join(" · ")||"–"; }
function renderPlaces(){
  const list=$("sesList"), q=query.toLowerCase();
  if(!rec.places.length){ list.innerHTML=`<div class="empty-msg">No places in this time range.</div>`; return; }
  const sys={}; for(const r of rec.sessions) for(const ch of r.chapters){ const k=ch.system||"Unknown"; sys[k]=(sys[k]||0)+Math.max(0,(chEnd(ch,r)-new Date(ch.start))/1000); }
  const ss=Object.entries(sys).sort((a,b)=>b[1]-a[1]), tot=ss.reduce((a,x)=>a+x[1],0)||1;
  const ps=rec.places.filter(p=>!q||(p.place+" "+(p.system||"")+" "+placeDid(p)).toLowerCase().includes(q));
  list.innerHTML=`<div class="srvsum"><div class="st"><b>${num(rec.places.length)}</b><span>places</span></div>
    <div class="st"><b>${num(rec.places.reduce((a,p)=>a+p.visits,0))}</b><span>stops</span></div>
    <div class="regions"><div class="rbar">${ss.map(([k,v])=>`<i style="--rc:${sc(k)};width:${(100*v/tot).toFixed(2)}%" title="${esc(k)}"></i>`).join("")}</div>
    <div class="rleg">${ss.map(([k,v])=>`<span style="--rc:${sc(k)}"><b>${esc(k)}</b> ${Math.round(100*v/tot)}% · ${hm(v)}</span>`).join("")}</div></div></div>
    <table class="srvtab plc"><thead><tr><th>Place</th><th>System</th><th class="num">Stops</th><th class="num">Sessions</th><th class="num">Time</th><th>What you did there</th><th>Last visit</th></tr></thead><tbody>`+
    ps.map(p=>`<tr data-place="${esc(p.place)}" title="Show the sessions with this place"><td><span class="pn">${hl(p.place)}</span></td>
      <td>${p.system?`<span class="sys" style="--sc:${sc(p.system)}">${esc(p.system)}</span>`:""}</td><td class="num">${num(p.visits)}</td><td class="num">${num(p.sessions)}</td>
      <td class="num">~${hm(p.seconds)}</td><td>${hl(placeDid(p))}</td><td>${shortDT(p.last)}</td></tr>`).join("")+`</tbody></table>`+
    (ps.length?"":`<div class="empty-msg">No place matches "${esc(query)}".</div>`)+
    `<p class="more">Times are estimates: the game logs when you arrive somewhere, not when you leave.</p>`;
}
function setRecView(v){ recView=v; $("btnRecap").classList.toggle("on",v==="sessions"); $("btnPlaces").classList.toggle("on",v==="places"); renderRecap(); $("sesList").scrollTop=0; }
$("btnRecap").addEventListener("click",()=>setRecView("sessions"));
$("btnPlaces").addEventListener("click",()=>setRecView("places"));
$("sesList").addEventListener("click",e=>{
  const row=e.target.closest("tr[data-place]");
  if(row){ $("search").value=row.dataset.place; setQuery(row.dataset.place); setRecView("sessions"); $("sesList").scrollTop=0; return; }
  const card=e.target.closest(".rc"); if(!card) return; const r=rec.sessions.find(x=>x.sid===card.dataset.sid); if(!r) return;
  const clear=()=>{ $("search").value=""; query=""; };   // the search found the stop; the events don't contain it
  if(e.target.closest('[data-act="events"]')){ clear(); setRange({preset:"custom",from:r.start,to:r.live?null:r.end,sid:r.sid}); showTab("ev"); return; }
  const chp=e.target.closest(".chp");
  if(chp){ if(window.getSelection().toString()) return; const ch=r.chapters[+chp.dataset.i]; const last=ch===r.chapters[r.chapters.length-1];
    clear(); setRange({preset:"custom",from:ch.start,to:r.live&&last?null:ch.end}); showTab("ev"); return; }
  if(e.target.closest(".rch")){ recOpen=recOpen||new Set(); recOpen.has(r.sid)?recOpen.delete(r.sid):recOpen.add(r.sid); renderRecap(); }
});
setInterval(()=>{ if(tab==="ses" && rec.sessions.some(r=>r.live)) renderRecap(); },60000);

/* ---------- history list ---------- */
function loadSessions(){ api("/api/sessions").then(res=>{ sessions=res.sessions||[]; renderSessions(); }); }
function renderSessions(){
  $("sesCount").textContent = sessions.length ? num(sessions.length)+" sessions" : "";
  $("sessions").innerHTML = sessions.length ? sessions.map(s=>{
    const d=new Date(s.first_ts);
    return `<div class="ses" data-id="${s.id}" title="${esc(s.path||"")}"><div><div class="a">${d.toLocaleDateString("en-GB",{weekday:"short",day:"numeric",month:"short",year:"numeric"})}${s.live?'<span class="badge">LIVE</span>':""}</div>
      <div class="b">${pad(d.getHours())}:${pad(d.getMinutes())} – ${s.live?"now":tLocal(s.last_ts).slice(0,5)} · ${dur(s.first_ts,s.last_ts)}</div></div>
      <div class="c">${num(s.events)} events${s.version?`<div class="b">${esc(s.version)}</div>`:""}</div></div>`; }).join("")
    : `<div class="empty-msg">${meta.import&&meta.import.running?"Importing ...":"No sessions yet."}</div>`;
  renderRange();
}
$("sessions").addEventListener("click",e=>{ const el=e.target.closest(".ses"); if(!el) return;
  const s=sessions.find(x=>x.id===el.dataset.id); if(!s) return;
  setRange({preset:"custom",from:s.first_ts,to:s.live?null:s.last_ts,sid:s.id}); });

/* ---------- header and status ---------- */
function renderState(){
  const s=state;
  $("hHandle").textContent=s.handle||"–"; $("hChannel").textContent=s.channel||"–"; $("hVersion").textContent=s.version||"–";
  updateCounts();
}
function renderMeta(){
  const parts=(meta.path||"").split(/[\\/]/); const short=parts.length>3?"…\\"+parts.slice(-3).join("\\"):(meta.path||"");
  $("hPath").textContent=meta.path?short:(meta.needs_setup?"Choose your Game.log ...":""); $("hPath").title=(meta.path||"")+"\nClick to change";
  document.title=(meta.mode==="replay"?"Replay · ":"")+"SC Log Tracker";
  $("btnSettings").style.display=meta.mode==="replay"?"none":"";
  const im=meta.import, ban=$("banner");
  if(im && im.running){ ban.classList.add("show"); $("bannerText").textContent=`Importing your history from Star Citizen's log backups: ${num(im.done)} of ${num(im.total)} logs ...`;
    $("bannerBar").firstElementChild.style.width=(im.total?Math.round(100*im.done/im.total):0)+"%"; $("bannerBar").style.display=""; }
  else if(meta.needs_setup){ ban.classList.add("show"); $("bannerText").textContent="Couldn't find your Game.log automatically. Open Settings to choose it."; $("bannerBar").style.display="none"; }
  else ban.classList.remove("show");
  if(lastImportRunning && !(im&&im.running)){ if(im&&im.imported) toast(`History updated: ${num(im.imported)} session(s) imported`); loadEvents(); loadSessions(); }
  lastImportRunning=!!(im&&im.running);
  if(meta.needs_setup && !$("settings").classList.contains("show") && !renderMeta.prompted){ renderMeta.prompted=true; openSettings(); }
  renderStatus();
}
function renderStatus(){
  const dot=$("dot"), txt=$("statusText");
  if(stopped) return;
  if(!connected){ dot.className="dot off"; txt.textContent="Tracker not reachable"; return; }
  const ago=(Date.now()-lastLineAt)/1000;
  if(meta.needs_setup){ dot.className="dot idle"; txt.textContent="Game.log not set"; }
  else if(meta.waiting){ dot.className="dot idle"; txt.textContent="Waiting for Star Citizen"; }
  else if(meta.mode==="replay"){ dot.className=meta.replay_done?"dot idle":"dot live"; txt.textContent=meta.replay_done?"Replay finished":"Replay running"; }
  else {
    // judge by the log's own clock: a fresh line means the game is running right now
    const logAgo = state.last_ts ? (Date.now()-new Date(state.last_ts))/1000 : Infinity;
    if(lastLineAt && ago<90 && logAgo<180){ dot.className="dot live"; txt.textContent="Live"; }
    else if(state.last_ts){ dot.className="dot idle"; txt.textContent="Game not running · last activity "+shortDT(state.last_ts); }
    else { dot.className="dot idle"; txt.textContent="Waiting for Star Citizen"; }
  }
}
setInterval(renderStatus,5000);

/* ---------- settings ---------- */
function openSettings(){ $("settings").classList.add("show"); $("setMsg").textContent="";
  $("setIntro").textContent = meta.needs_setup ? "Couldn't find your Game.log automatically. Pick the Game.log file, or your StarCitizen or LIVE folder." : "Where Star Citizen writes its log. Pick the Game.log file, or your StarCitizen or LIVE folder.";
  api("/api/config").then(c=>{
    $("setPath").value=c.log||"";
    $("setDefault").textContent=c.default_log||""; $("setDefault").dataset.path=c.default_log||"";
    const h=c.history||{}; $("setHistory").textContent=`${num(h.sessions)} sessions · ${num(h.events)} events`+(h.oldest?` since ${new Date(h.oldest).toLocaleDateString("en-GB",{day:"numeric",month:"short",year:"numeric"})}`:"")+` · stored in ${c.data_dir}`;
  });
}
function closeSettings(){ $("settings").classList.remove("show"); const q=$("btnQuit"); q.classList.remove("armed"); q.textContent="Quit SC Log Tracker"; }
$("btnSettings").addEventListener("click",openSettings); $("hPath").addEventListener("click",()=>{ if(meta.mode!=="replay") openSettings(); });
$("setClose").addEventListener("click",closeSettings);
$("settings").addEventListener("click",e=>{ if(e.target.id==="settings") closeSettings(); });
document.addEventListener("keydown",e=>{ if(e.key==="Escape") closeSettings(); });
$("setDefault").addEventListener("click",()=>{ const p=$("setDefault").dataset.path; if(p){ $("setPath").value=p; saveLog(); } });
function msg(id,text,cls){ const m=$(id); m.textContent=text; m.className="msg "+(cls||""); }
function saveLog(){ const v=$("setPath").value.trim(); if(!v){ msg("setMsg","Enter a path first.","err"); return; }
  $("setSave").disabled=true; msg("setMsg","Checking ...");
  api("/api/config",{log:v}).then(r=>{ $("setSave").disabled=false;
    if(r.error){ msg("setMsg",r.error,"err"); return; }
    $("setPath").value=r.log; msg("setMsg","Saved. Now following "+r.log,"ok"); toast("Game.log set"); loadEvents(); loadSessions(); }); }
$("setSave").addEventListener("click",saveLog);
$("setPath").addEventListener("keydown",e=>{ if(e.key==="Enter") saveLog(); });
$("setBrowse").addEventListener("click",()=>{ $("setBrowse").disabled=true; msg("setMsg","A file dialog opened. If you don't see it, check your taskbar.");
  api("/api/browse",{}).then(r=>{ $("setBrowse").disabled=false;
    if(r.error) msg("setMsg",r.error,"err"); else if(r.path){ $("setPath").value=r.path; saveLog(); } else msg("setMsg",""); }); });
$("btnQuit").addEventListener("click",()=>{ const q=$("btnQuit");
  if(!q.classList.contains("armed")){ q.classList.add("armed"); q.textContent="Click again to quit"; setTimeout(()=>{ q.classList.remove("armed"); q.textContent="Quit SC Log Tracker"; },4000); return; }
  stopped=true; api("/api/quit",{}).finally(()=>{ closeSettings(); $("stopped").classList.add("show"); }); });

/* ---------- connection ---------- */
function handle(m){
  if(m.t==="snapshot"){ if(m.v && m.v!=="{{VERSION}}"){ location.reload(); return; }   // a newer version took over
    raw=m.raw; state=m.state; meta=m.meta; lastLineAt=meta.last_read||0;
    renderMeta(); renderState(); renderRange(); if(tab==="raw") renderRaw(); loadEvents(); loadSessions(); }
  else if(m.t==="batch"){
    if(m.raw.length){ raw.push(...m.raw); if(raw.length>MAX_RAW) raw.splice(0,raw.length-MAX_RAW); addRaw(m.raw); lastLineAt=Date.now(); }
    state=m.state; renderState(); if(m.ev.length){ addEvents(m.ev); if(inRange(m.ev[m.ev.length-1])) recSoon(); } renderStatus();
    if(m.ev.some(e=>e.ti==="Joined server"||/^(Exited to menu|Disconnected|Game closed|Crash!|Connection error)/.test(e.ti))) loadServers();
  }
  else if(m.t==="reset"){ raw=[]; state=m.state||{}; if(m.meta) meta=m.meta; renderMeta(); renderState(); if(tab==="raw") renderRaw(); if(m.note) toast(m.note); loadSessions(); }
  else if(m.t==="meta"){ meta=m.meta; renderMeta(); }
  else if(m.t==="sessions"){ loadSessions(); }
}
function connect(){
  const es=new EventSource("/stream");
  es.onopen=()=>{ connected=true; renderStatus(); };
  es.onmessage=e=>{ try{ handle(JSON.parse(e.data)); }catch(err){ console.error(err); } };
  es.onerror=()=>{ connected=false; renderStatus(); };
}
$("btnExport").addEventListener("click",()=>{
  const b=bounds();
  const blob=new Blob([JSON.stringify({exported:new Date().toISOString(),tool:"SC Log Tracker {{VERSION}}",range:b,total,events},null,1)],{type:"application/json"});
  const a=document.createElement("a"); const d=new Date();
  a.download=`sc_events_${d.toISOString().slice(0,16).replace(/[:T]/g,"-")}.json`; a.href=URL.createObjectURL(blob); a.click();
  setTimeout(()=>URL.revokeObjectURL(a.href),2000);
});
renderRange();
try{ showTab(localStorage.getItem("sclt.tab")||"ev"); }catch(e){ showTab("ev"); }
connect();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
