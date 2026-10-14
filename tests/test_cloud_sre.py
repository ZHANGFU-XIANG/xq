import datetime as dt
import gzip
import pickle

import pandas as pd

from src import schedule, snapshot
from src.agents import search_agent
from src.api import sre
from src.config import TW_TZ


def test_snapshot_pack_roundtrip_and_target_check(tmp_path, monkeypatch):
    idx = pd.bdate_range(end="2026-10-14", periods=600)
    df = pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1.0}, index=idx)
    ctx = {"panel": {"2330": df}, "codes": ["2330"], "etf": set(), "taiex": df, "us_hist": {}, "alpha": {"predictions": {}},
           "target": dt.date(2026, 10, 15)}
    obj = pickle.loads(gzip.decompress(snapshot.pack(ctx)))
    assert len(obj["panel"]["2330"]) == snapshot.SNAP_DAYS and obj["target"] == dt.date(2026, 10, 15)
    monkeypatch.setattr(snapshot, "CACHE_DIR", tmp_path)
    (tmp_path / snapshot.ASSET).write_bytes(snapshot.pack(ctx))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert snapshot.fetch(dt.date(2026, 10, 15))["codes"] == ["2330"]
    assert snapshot.fetch(dt.date(2026, 10, 16)) is None  # 目標日不符就自己算


def test_sre_flags_missed_schedule(monkeypatch):
    monkeypatch.setattr(schedule, "load_log", lambda: {"2026-10-14": {"premarket": "07:01"}})
    now = dt.datetime(2026, 10, 14, 23, 50, tzinfo=TW_TZ)
    keys = [e["title"] for e in sre.check_schedule(now)]
    assert any("postclose" in k for k in keys) and not any("premarket" in k for k in keys)


def test_search_normalize():
    s = search_agent.normalize("HTTPSConnectionPool(host='x', port=443): Read timed out. (read timeout=30) https://a.b/c")
    assert "443" not in s and "https" not in s and "Read timed out" in s
