import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sc_log_tracker  # noqa: E402

DEMO_LOG = ROOT / "examples" / "demo_game.log"


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Never touch the real settings or history while testing."""
    monkeypatch.setattr(sc_log_tracker, "DATA_DIR", tmp_path / "data")
    return tmp_path / "data"
