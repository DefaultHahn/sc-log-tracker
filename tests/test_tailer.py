"""Live tailing: chunked writes and a new game session."""
import random
import shutil
import time

from conftest import DEMO_LOG
from helpers import full_parse, live_hub, wait_for


def test_chunked_live_writes_match_full_parse(tmp_path):
    log = tmp_path / "Game.log"
    log.write_bytes(b"")
    hub = live_hub(log)
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
    # every event ended up in the history store exactly once
    assert wait_for(lambda: hub.store.events()[1] == expected.seq)


def test_new_session_resets(tmp_path):
    log = tmp_path / "Game.log"
    shutil.copy(DEMO_LOG, log)
    hub = live_hub(log)
    assert wait_for(lambda: hub.meta["history_done"] and hub.parser.state["handle"] == "DemoPilot")
    first_sid = hub.sid
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
    assert wait_for(lambda: hub.sid not in (None, first_sid))
