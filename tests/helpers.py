import json
import shutil
import threading
import time
import urllib.error
import urllib.request

from conftest import DEMO_LOG

import sc_log_tracker as t

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def wait_for(cond, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def full_parse(path):
    p = t.Parser()
    with open(path, "rb") as f:
        for raw in f:
            p.feed(raw.decode("utf-8").rstrip("\r\n"))
    return p


def make_install(root, backups=0):
    """A fake StarCitizen/LIVE folder with the demo log as Game.log and n distinct backups."""
    live = root / "StarCitizen" / "LIVE"
    (live / "logbackups").mkdir(parents=True)
    shutil.copy(DEMO_LOG, live / "Game.log")
    data = DEMO_LOG.read_bytes()
    for i in range(backups):
        # a different first line (start time) makes it a different session, like real backups
        body = data.replace(b"2026-10-03T18:", f"2026-09-{10 + i:02d}T18:".encode())
        (live / "logbackups" / f"Game Build(12660092) {10 + i:02d} Sep 26 (20 00 00).log").write_bytes(body)
    return live


def live_hub(log):
    hub = t.Hub(t.Store(":memory:"), "live", quiet=True)
    hub.switch_log(log, remember=False)
    return hub


def serve(hub, port=18777):
    srv, port = t.start_server(hub, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def request(port, path, body=None, headers=None):
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=hdrs)
    try:
        with OPENER.open(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
