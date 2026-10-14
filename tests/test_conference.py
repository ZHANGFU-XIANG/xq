import datetime as dt

import pandas as pd

from src.agents import conference


def test_react_day():
    # 下午 1:30 收盤後召開 → 隔天才反應；盤中召開 → 當天反應
    assert conference._react_day(dt.date(2026, 10, 15), "14:00") == "隔天"
    assert conference._react_day(dt.date(2026, 10, 15), "10:00") == "當天"
    assert conference._react_day(dt.date(2026, 10, 15), "") == "隔天"


def test_upcoming_filters_codes_and_window(monkeypatch):
    cal = pd.DataFrame([
        {"code": "2330", "date": "2026-10-15", "time": "14:00", "summary": "Q3", "file_date": ""},
        {"code": "2317", "date": "2026-11-20", "time": "15:00", "summary": "nan", "file_date": ""},   # 超過 14 天
        {"code": "2303", "date": "2026-10-16", "time": "17:00", "summary": "x", "file_date": ""},    # 不在清單
        {"code": "2881", "date": "2026-10-10", "time": "10:00", "summary": "x", "file_date": ""},    # 已過
    ])
    monkeypatch.setattr(conference, "calendar", lambda months_ahead=1: cal)
    out = conference.upcoming(["2330", "2317", "2881"], days=14, today=dt.date(2026, 10, 14))
    assert [x["code"] for x in out] == ["2330"]
    assert out[0]["days"] == 1 and out[0]["react"] == "隔天"
