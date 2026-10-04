"""Live tailing: chunked writes, a new session, and the web endpoints."""
import json
import random
import shutil
import threading
import time
import urllib.request

from conftest import DEMO_LOG

import sc_log_tracker as t


def wait_for(cond, timeout=10):
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


def test_chunked_live_writes_match_full_parse(tmp_path):
    log = tmp_path / "Game.log"
    log.write_bytes(b"")
    hub = t.Hub(log, "live", quiet=True)
    t.FileTailer(log, hub).start()
    data = DEMO_LOG.read_bytes()
    rnd = random.Random(1)
    i = 0
    with open(log, "ab") as f:
        while i < len(data):
            n = rnd.randint(1, 900)            # splits lines and multi-line notices
            f.write(data[i:i + n])
            f.flush()
            i += n
            if rnd.random() < 0.05:
                time.sleep(0.3)
    expected = full_parse(DEMO_LOG)
    assert wait_for(lambda: hub.parser.line_no == expected.line_no)
    assert hub.parser.seq == expected.seq
    assert hub.parser.state["qt_jumps"] == expected.state["qt_jumps"]


def test_new_session_resets(tmp_path):
    log = tmp_path / "Game.log"
    shutil.copy(DEMO_LOG, log)
    hub = t.Hub(log, "live", quiet=True)
    t.FileTailer(log, hub).start()
    assert wait_for(lambda: hub.meta["history_done"] and hub.parser.state["handle"] == "DemoPilot")
    time.sleep(0.5)
    # the game moves the old log away and starts a fresh one
    for _ in range(50):                        # Windows: the tailer may have it open for a moment
        try:
            log.unlink()
            break
        except PermissionError:
            time.sleep(0.05)
    time.sleep(0.6)
    log.write_bytes(b'<2026-10-04T10:00:00.000Z> FileVersion: 4.10.193.11644\r\n')
    assert wait_for(lambda: hub.parser.line_no == 1)
    assert hub.parser.state["handle"] is None


def test_web_endpoints(tmp_path):
    log = tmp_path / "Game.log"
    shutil.copy(DEMO_LOG, log)
    hub = t.Hub(log, "live", quiet=True)
    t.FileTailer(log, hub).start()
    srv, port = t.start_server(hub, 18777)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert wait_for(lambda: hub.meta["history_done"])
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode("utf-8")
        assert "SC Log Tracker" in page and t.__version__ in page
        state = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5).read())
        assert state["state"]["handle"] == "DemoPilot"
    finally:
        srv.shutdown()
