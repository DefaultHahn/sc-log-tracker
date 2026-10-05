"""Session recaps: stops, what happened there and the places overview."""
from conftest import DEMO_LOG

import sc_log_tracker as t


def ev(ts, c, ti, d="", x=None):
    return {"ts": f"2026-10-02T{ts}.000Z", "c": c, "ti": ti, "d": d, "x": x}


def recap(events, first="10:00:00", last="14:00:00", handle="Me"):
    sess = {"id": "s1", "first_ts": f"2026-10-02T{first}.000Z", "last_ts": f"2026-10-02T{last}.000Z",
            "handle": handle, "version": "4.10"}
    return t.build_recap(sess, events)


def lines(stop):
    return [line["t"] for line in stop["lines"]]


def demo_store():
    store = t.Store(":memory:")
    sid = t.session_key(t.first_line_of(DEMO_LOG))
    store.begin_session(sid, DEMO_LOG)
    p, events = t.Parser(), []
    with open(DEMO_LOG, "rb") as f:
        for raw in f:
            events.extend(p.feed(raw.decode("utf-8").rstrip("\r\n")))
    store.add_events(sid, events)
    store.update_session(sid, p.state, 1)
    return store


def test_demo_session_recap():
    (r,), places = demo_store().recap()
    assert r["summary"]["route"] == ["Orbituary", "Checkmate", "Ashgrove (Monox)"]
    orbituary, checkmate, outpost = r["chapters"]
    assert orbituary["system"] == "Pyro" and "Accepted: Hostile Takeover" in lines(orbituary)
    assert "Bought 5× CureLife Healing · 1,325 aUEC" in lines(orbituary)
    assert checkmate["via"] == "quantum"
    assert "Completed: Hostile Takeover" in lines(outpost)
    assert "2× Fuse inserted · door" in lines(outpost)
    assert "Hacking chip used · level 1 access" in lines(outpost)
    s = r["summary"]
    assert s["party"] == ["NovaRider", "Wingman_Alpha"] and s["spent"] == 1325 and s["deaths"] == 1
    assert s["contracts"] == {"accepted": 1, "completed": 1, "failed": 0, "abandoned": 0}
    assert {p["place"] for p in places} == {"Orbituary", "Checkmate", "Ashgrove (Monox)"}


def test_place_reported_right_after_a_server_change_is_ignored():
    r = recap([
        ev("10:00:00", "travel", "Location", "Lazarus", "Pyro"),
        ev("10:30:00", "social", "Party launch", "started by Friend"),
        ev("10:31:00", "session", "Joined server", "pub_use1b_1_2 · US East", "pub_use1b_1_2"),
        ev("10:31:30", "travel", "Location", "Starlight Service Station", "Pyro"),   # home station blip
        ev("10:33:00", "combat", "Emergency services en route"),
        ev("10:34:00", "travel", "Location", "Lazarus", "Pyro"),
        ev("11:00:00", "activity", "Valakkar egg delivered", "", "egg"),
    ], last="11:10:00")
    assert [c["place"] for c in r["chapters"]] == ["Lazarus"]
    assert r["chapters"][0]["seconds"] == 70 * 60
    assert "Downed, emergency services called" in lines(r["chapters"][0])
    assert r["summary"]["route"] == ["Lazarus"]


def test_a_real_stop_in_between_is_kept():
    r = recap([
        ev("10:00:00", "travel", "Location", "Lazarus", "Pyro"),
        ev("10:30:00", "travel", "Location", "Starlight Service Station", "Pyro"),
        ev("10:45:00", "economy", "Purchase", "2x Medpen for 500 aUEC", "500"),
        ev("11:00:00", "travel", "Location", "Lazarus", "Pyro"),
    ], last="11:30:00")
    assert [c["place"] for c in r["chapters"]] == ["Lazarus", "Starlight Service Station", "Lazarus"]


def test_short_empty_stops_are_left_out():
    r = recap([
        ev("10:00:00", "travel", "Location", "Orbituary", "Pyro"),
        ev("10:20:00", "travel", "Location", "Pyro System", "Pyro"),
        ev("10:20:30", "travel", "Location", "Checkmate", "Pyro"),
    ], last="10:40:00")
    assert [c["place"] for c in r["chapters"]] == ["Orbituary", "Checkmate"]
    assert r["chapters"][0]["end"] == "2026-10-02T10:20:30.000Z"


def test_mission_outcome_is_counted_once_with_its_name():
    r = recap([
        ev("10:00:00", "travel", "Location", "Levski", "Nyx"),
        ev("10:01:00", "mission", "Contract accepted", "Bulk Haul"),
        ev("10:40:00", "mission", "Contract complete", "Bulk Haul"),
        ev("10:40:02", "mission", "Mission ended", "completed", "completed"),
        ev("10:50:00", "mission", "Mission ended", "abandoned: Research", "abandoned"),
        ev("10:55:00", "mission", "Mission ended", "abandoned: Research", "abandoned"),
    ], last="11:00:00")
    assert lines(r["chapters"][0]) == ["Accepted: Bulk Haul", "Completed: Bulk Haul", "Abandoned: Research (2×)"]
    assert r["summary"]["contracts"] == {"accepted": 1, "completed": 1, "failed": 0, "abandoned": 2}


def test_purchases_are_added_up():
    r = recap([
        ev("10:00:00", "travel", "Location", "Levski", "Nyx"),
        ev("10:05:00", "economy", "Purchase", "Hacking Chip for 2,550 aUEC  (Electronics)", "2550"),
        ev("10:05:05", "economy", "Purchase", "Hacking Chip for 2,550 aUEC  (Electronics)", "2550"),
        ev("10:06:00", "economy", "Purchase", "71x Rifle Mag for 22,862 aUEC  (Weapons)", "22862"),
    ], last="10:30:00")
    assert lines(r["chapters"][0]) == ["Bought 2× Hacking Chip, 71× Rifle Mag · 27,962 aUEC"]
    assert r["summary"]["spent"] == 27962 and r["summary"]["buys"] == 3


def test_server_change_and_reconnect():
    r = recap([
        ev("10:00:00", "session", "Joined server", "", "pub_euw1b_1_100"),
        ev("10:01:00", "travel", "Location", "Grim HEX", "Stanton"),
        ev("10:20:00", "session", "Joined server", "", "pub_euw1b_1_100"),
        ev("10:40:00", "session", "Joined server", "", "pub_use1b_1_200"),
    ], last="11:00:00")
    assert lines(r["chapters"][0]) == ["Changed server · US East", "Reconnected to the same server"]
    assert [s["region"] for s in r["summary"]["servers"]] == ["Europe", "US East"]


def test_party_members_without_yourself():
    r = recap([
        ev("10:00:00", "travel", "Location", "Grim HEX", "Stanton"),
        ev("10:01:00", "social", "Party invite", "from Friend"),
        ev("10:02:00", "social", "Party launch", "started by Me"),
        ev("10:03:00", "social", "Party member online", "Other"),
    ], last="10:30:00")
    assert r["summary"]["party"] == ["Friend", "Other"]


def test_places_overview_adds_up_visits():
    a = recap([ev("10:00:00", "travel", "Location", "Orbituary", "Pyro"),
               ev("10:30:00", "activity", "Fuse inserted", "contested zone relay", "fuse")], last="11:00:00")
    b = dict(recap([ev("12:00:00", "travel", "Location", "Orbituary", "Pyro")], first="12:00:00", last="12:30:00"),
             sid="s2")
    (p,) = t.places_overview([a, b])
    assert p["place"] == "Orbituary" and p["visits"] == 2 and p["sessions"] == 2
    assert p["seconds"] == 90 * 60 and p["acts"] == {"Fuse inserted": 1}
