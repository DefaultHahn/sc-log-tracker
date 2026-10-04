"""History store, backfill import of logbackups and parser upgrades."""
import shutil

from conftest import DEMO_LOG
from helpers import full_parse, live_hub, make_install, wait_for

import sc_log_tracker as t


def demo_events():
    return full_parse_events(DEMO_LOG)


def full_parse_events(path):
    p, out = t.Parser(), []
    with open(path, "rb") as f:
        for raw in f:
            out.extend(p.feed(raw.decode("utf-8").rstrip("\r\n")))
    return p, out


def test_store_ignores_duplicates():
    store = t.Store(":memory:")
    p, evs = demo_events()
    store.begin_session("s1", "x")
    assert len(store.add_events("s1", evs)) == len(evs)
    assert store.add_events("s1", evs) == []
    store.update_session("s1", p.state, 123)
    s = store.session("s1")
    assert s["events"] == len(evs) and s["handle"] == "DemoPilot" and s["size"] == 123


def test_store_time_range():
    store = t.Store(":memory:")
    _, evs = demo_events()
    store.begin_session("s1", "x")
    store.add_events("s1", evs)
    all_events, total = store.events()
    assert total == len(evs) and all_events[0]["ts"] <= all_events[-1]["ts"]
    some, n = store.events("2026-10-03T18:10:00.000Z", "2026-10-03T18:13:00.000Z")
    assert 0 < n < total
    assert all("2026-10-03T18:10" <= e["ts"] <= "2026-10-03T18:13:00.000Z" for e in some)
    newest, n = store.events(limit=5)
    assert len(newest) == 5 and n == total and newest[-1]["ts"] == all_events[-1]["ts"]


def test_parser_upgrade_reimports(tmp_path):
    db = tmp_path / "h.db"
    store = t.Store(db)
    _, evs = demo_events()
    store.begin_session("s1", "x")
    store.add_events("s1", evs[:5])
    store.db.execute("UPDATE sessions SET parser=?", (t.PARSER_VERSION - 1,))
    store.db.commit()
    store.begin_session("s1", "x")              # older parser: old events are dropped
    assert store.events()[1] == 0
    assert store.session("s1")["parser"] == t.PARSER_VERSION


def test_backfill_imports_logbackups(tmp_path):
    live = make_install(tmp_path, backups=3)
    hub = live_hub(live / "Game.log")
    assert wait_for(lambda: hub.meta.get("import") and not hub.meta["import"]["running"]
                    and hub.meta["history_done"])
    assert hub.meta["import"]["imported"] == 3
    sessions = hub.store.sessions()
    assert len(sessions) == 4                   # 3 backups + the live session
    per_session = full_parse(DEMO_LOG).seq
    assert hub.store.events()[1] == 4 * per_session

    # restarting imports nothing new
    hub2 = t.Hub(hub.store, "live", quiet=True)
    hub2.switch_log(live / "Game.log", remember=False)
    assert wait_for(lambda: hub2.meta.get("import") and not hub2.meta["import"]["running"])
    assert hub2.meta["import"]["imported"] == 0
    assert hub2.store.events()[1] == 4 * per_session


def test_live_session_is_not_imported_twice_after_rotation(tmp_path):
    live = make_install(tmp_path)
    hub = live_hub(live / "Game.log")
    per_session = full_parse(DEMO_LOG).seq
    assert wait_for(lambda: hub.store.events()[1] == per_session)
    for tr in (hub.source, hub.importer):
        tr.stop()
    shutil.move(live / "Game.log", live / "logbackups" / "Game Build(1) 03 Oct 26 (20 00 00).log")
    hub2 = t.Hub(hub.store, "live", quiet=True)
    hub2.switch_log(live / "Game.log", remember=False)
    assert wait_for(lambda: hub2.meta.get("import") and not hub2.meta["import"]["running"])
    assert hub2.meta["import"]["imported"] == 0
    assert hub2.store.events()[1] == per_session


def test_session_key_uses_first_line():
    a = b"<2026-10-03T18:00:00.000Z> BackupNameAttachment=x\r\n"
    assert t.session_key(a) == t.session_key(a.strip())
    assert t.session_key(a) != t.session_key(b"<2026-10-04T18:00:00.000Z> BackupNameAttachment=x")
