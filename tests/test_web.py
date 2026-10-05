"""Web endpoints, settings and protection against requests from other websites."""
import json

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
    assert len(sessions) == 2 and sum(s["live"] for s in sessions) == 1


def test_config_get_and_set(running, tmp_path):
    hub, port, live = running
    cfg = json.loads(request(port, "/api/config")[1])
    assert cfg["log"].endswith("Game.log") and cfg["history"]["sessions"] == 2
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
    assert t.find_running_instance(port) == port


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
