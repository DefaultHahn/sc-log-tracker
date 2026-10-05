"""Web endpoints, settings and protection against requests from other websites."""
import http.client
import json
import threading

import pytest
from helpers import OPENER, live_hub, make_install, request, serve, wait_for

import sc_log_tracker as t


@pytest.fixture
def running(tmp_path):
    live = make_install(tmp_path, backups=1)
    hub = live_hub(live / "Game.log")
    srv, port = serve(hub)
    assert wait_for(lambda: hub.meta["history_done"] and hub.meta.get("import")
                    and not hub.meta["import"]["running"])
    yield hub, port, live
    srv.shutdown()
    srv.server_close()


def test_page_and_state(running):
    hub, port, _ = running
    code, body = request(port, "/")
    assert code == 200 and b"SC Log Tracker" in body and t.__version__.encode() in body
    code, body = request(port, "/api/state")
    assert json.loads(body)["state"]["handle"] == "DemoPilot"


def test_events_and_sessions(running):
    hub, port, _ = running
    data = json.loads(request(port, "/api/events")[1])
    assert data["total"] == len(data["events"]) > 0
    some = json.loads(request(port, "/api/events?from=2026-10-03T18:10:00.000Z&to=2026-10-03T18:12:00.000Z")[1])
    assert 0 < some["total"] < data["total"]
    sessions = json.loads(request(port, "/api/sessions")[1])["sessions"]
    assert len(sessions) == 2 and not any(s["live"] for s in sessions)   # the demo game isn't running now


def test_config_get_and_set(running, tmp_path):
    hub, port, live = running
    cfg = json.loads(request(port, "/api/config")[1])
    assert cfg["log"].endswith("Game.log") and cfg["history"]["sessions"] == 2
    assert cfg["default_log"].endswith("Game.log") and "autostart" not in cfg
    hdr = {t.CSRF_HEADER: "1"}
    code, body = request(port, "/api/config", {"log": str(tmp_path / "nowhere")}, hdr)
    assert code == 400 and "Couldn't find" in json.loads(body)["error"]
    code, body = request(port, "/api/config", {"log": str(live.parent)}, hdr)   # the StarCitizen folder
    assert code == 200 and json.loads(body)["log"].endswith("Game.log")
    assert json.loads(t.config_path().read_text())["log"].endswith("Game.log")


def test_posts_need_the_custom_header(running):
    _, port, _ = running
    code, _ = request(port, "/api/quit", {})                 # no header: a website can't do this
    assert code == 403
    code, _ = request(port, "/api/config", {"log": "x"})
    assert code == 403


def test_foreign_host_is_rejected(running):
    _, port, _ = running
    code, _ = request(port, "/api/state", headers={"Host": "evil.example:80"})
    assert code == 403


def test_single_instance_is_found(running):
    _, port, _ = running
    assert t.find_running_instance(port) == (port, t.__version__)


def test_quit_from_dashboard(running, monkeypatch):
    _, port, _ = running
    quit_called = threading.Event()
    monkeypatch.setattr(t, "exit_app", quit_called.set)
    code, body = request(port, "/api/quit", {}, {t.CSRF_HEADER: "1"})
    assert code == 200 and json.loads(body)["ok"]
    assert quit_called.wait(5)


@pytest.fixture
def instance(tmp_path, monkeypatch):
    """A running tracker whose 'quit' stops its web server, like the real process exiting."""
    live = make_install(tmp_path)
    hub = live_hub(live / "Game.log")
    srv, port = serve(hub, 18850)

    def fake_exit():
        srv.shutdown()
        srv.server_close()
    monkeypatch.setattr(t, "exit_app", fake_exit)
    yield srv, port
    fake_exit()


def test_an_older_version_is_replaced(instance):
    _, port = instance
    assert t.take_over_or_open(port, open_browser=False, version="99.0.0") is False
    assert t.ping(port) is None                       # the old one quit, the new one can start


def test_the_same_or_newer_version_is_opened(instance):
    _, port = instance
    assert t.take_over_or_open(port, open_browser=False) is True
    assert t.take_over_or_open(port, open_browser=False, version="0.9.0") is True
    assert t.ping(port)["version"] == t.__version__   # still running


def test_nothing_running():
    assert t.find_running_instance(19900) is None
    assert t.take_over_or_open(19900, open_browser=False) is False


def test_version_tuple():
    assert t.version_tuple("1.10.0") > t.version_tuple("1.9.3")
    assert t.version_tuple("1.2.1") > t.version_tuple("1.2.0") > t.version_tuple("1.1")
    assert t.version_tuple("2.0.0rc1") == (2, 0, 0)
    assert t.version_tuple(None) == (0,) and t.version_tuple("x") == (0,)


def test_resolve_user_path(tmp_path):
    live = make_install(tmp_path)
    assert t.resolve_user_path(str(live / "Game.log")) == live / "Game.log"
    assert t.resolve_user_path(str(live)) == live / "Game.log"
    assert t.resolve_user_path(str(live.parent)) == live / "Game.log"
    assert t.resolve_user_path(f'"{live}"') == live / "Game.log"
    assert t.resolve_user_path(str(tmp_path / "missing" / "Game.log")) is None
    assert t.resolve_user_path("") is None


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as e:
        t.main(["--version"])
    assert e.value.code == 0
    assert t.__version__ in capsys.readouterr().out


def test_ping_without_proxy(running):
    _, port, _ = running
    with OPENER.open(f"http://127.0.0.1:{port}/api/ping", timeout=5) as r:
        assert json.loads(r.read())["app"] == t.APP_NAME


def test_servers_endpoint(running):
    _, port, _ = running
    data = json.loads(request(port, "/api/servers")[1])
    assert len(data["visits"]) == 2                  # live session + one backup
    assert sum(v["live"] for v in data["visits"]) == 0   # the live session already ended (game closed)
    assert data["regions"][0]["region"] == "Europe"
    assert data["servers"][0]["shard"] == "pub_euw1b_12660092_100" and data["servers"][0]["visits"] == 2


def test_closed_connections_are_not_logged(capsys):
    srv = t.Server(("127.0.0.1", 0), t.Handler)
    try:
        try:
            raise ConnectionAbortedError("tab closed")
        except ConnectionAbortedError:
            srv.handle_error(None, ("127.0.0.1", 1))
        assert capsys.readouterr().err == ""
        try:
            raise ValueError("a real bug")
        except ValueError:
            srv.handle_error(None, ("127.0.0.1", 1))
        assert "a real bug" in capsys.readouterr().err
    finally:
        srv.server_close()


def open_dashboard(port):
    """Connect like a browser tab: open the event stream and read the first message."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/stream")
    resp = conn.getresponse()
    assert resp.status == 200 and resp.fp.readline().startswith(b"data: ")
    return conn, resp


def close_dashboard(tab):
    conn, resp = tab
    resp.close()
    conn.close()


def test_closing_the_dashboard_quits(running, monkeypatch):
    hub, port, _ = running
    quit_called = threading.Event()
    monkeypatch.setattr(t, "exit_app", quit_called.set)
    assert hub.dashboard_closed_for() is None           # no dashboard yet: never quit on its own
    t.AutoQuit(hub, grace=0.5).start()
    tab = open_dashboard(port)
    assert wait_for(lambda: hub.clients) and hub.dashboard_closed_for() is None
    assert not quit_called.wait(1.5)                     # open tab: keeps running
    close_dashboard(tab)
    assert wait_for(lambda: hub.dashboard_closed_for() is not None, timeout=5)   # noticed within ~1 s
    assert quit_called.wait(5)


def test_reloading_the_dashboard_doesnt_quit(running, monkeypatch):
    hub, port, _ = running
    quit_called = threading.Event()
    monkeypatch.setattr(t, "exit_app", quit_called.set)
    t.AutoQuit(hub, grace=3).start()
    tab = open_dashboard(port)
    close_dashboard(tab)
    assert wait_for(lambda: hub.dashboard_closed_for() is not None, timeout=5)
    tab = open_dashboard(port)                           # the reloaded page connects again
    assert not quit_called.wait(4)
    close_dashboard(tab)


def test_recap_endpoint(running):
    hub, port, _ = running
    data = json.loads(request(port, "/api/recap")[1])
    assert len(data["sessions"]) == 2 and not any(r["live"] for r in data["sessions"])
    newest = data["sessions"][0]
    assert [c["place"] for c in newest["chapters"]] == ["Orbituary", "Checkmate", "Ashgrove (Monox)"]
    assert newest["summary"]["contracts"]["completed"] == 1
    places = {p["place"]: p for p in data["places"]}
    assert places["Orbituary"]["visits"] == 2 and places["Orbituary"]["sessions"] == 2
    some = json.loads(request(port, "/api/recap?from=2026-10-03T18:00:00.000Z")[1])
    assert len(some["sessions"]) == 1


def test_old_autostart_entry_only_cleans_up():
    # Versions before 1.4 started "--background" with Windows; now that just removes the entry.
    assert t.main(["--background", "--port", "18990"]) is None
    assert t.ping(18990) is None
