import datetime as dt

import pandas as pd

from src.agents import alerts, execution, shadow
from src.config import TW_TZ
from src.state import portfolio as pfm


def test_actual_breakdown_timing_vs_open(monkeypatch):
    idx = pd.DatetimeIndex(["2026-10-05"])
    panel = {"6213": pd.DataFrame({"Open": [690.0], "Close": [700.0]}, index=idx)}
    monkeypatch.setattr(pfm, "load_trades", lambda: [
        {"date": "2026-10-05", "code": "6213", "side": "BUY", "shares": "1000", "price": "726", "asset": "STOCK"},
        {"date": "2026-10-05", "code": "6213", "side": "BUY", "shares": "5", "price": "700", "asset": "STOCK"}])
    b = shadow.actual_breakdown(dt.date(2026, 10, 5), dt.date(2026, 10, 5), panel)
    assert b["timing"] == -(36 * 1000 + 10 * 5)          # 買在開盤價之上 → 負數
    assert b["fragment_fees"] < 0                         # 5 股零股付最低手續費 20 元


def test_open_limits_use_latest_close_before_open(monkeypatch):
    q = {"6196": {"price": 656.0, "prev_close": 641.0, "date": "20261014"}}
    monkeypatch.setattr(alerts, "realtime", lambda codes: q)
    lim = execution.open_limits([{"code": "6196", "action": "BUY", "shares": 2000, "limit_price": 661.0}],
                                dt.date(2026, 10, 15))[0]
    assert lim["prev_close"] == 656.0 and lim["open_limit"] == 675.0 and "開盤價成交" in lim["text"]
    odd = execution.open_limits([{"code": "6196", "action": "BUY", "shares": 158, "limit_price": 661.0}], dt.date(2026, 10, 15))[0]
    assert odd["odd"] and "09:10" in odd["text"]


def test_preopen_only_in_window():
    assert execution.preopen_alerts(dt.datetime(2026, 10, 15, 8, 20, tzinfo=TW_TZ)) == []
    assert execution.preopen_alerts(dt.datetime(2026, 10, 18, 8, 45, tzinfo=TW_TZ)) == []  # 週日


def test_fin_report_ytd_and_flags(monkeypatch):
    from src.agents import fin_report

    monkeypatch.setattr(fin_report, "_seen", lambda: {("9999", 2025, 2): {"eps": "2.0", "revenue": "1000"}})
    d = fin_report.assess({"code": "9999", "year": 2026, "quarter": 2, "eps": 3.0, "revenue": 1200.0, "gross_profit": 300.0})
    assert d["eps_yoy_pct"] == 50.0 and d["rev_yoy_pct"] == 20.0 and "財報利多" in d["flags"] and "上半年累計" in d["text"]
    monkeypatch.setattr(fin_report, "_seen", lambda: {("9999", 2025, 2): {"eps": "1.0", "revenue": "1000"}})
    d = fin_report.assess({"code": "9999", "year": 2026, "quarter": 2, "eps": -0.5, "revenue": 900.0})
    assert "轉虧" in d["flags"] and "財報利空" in d["flags"]


def test_presentation_script_fits_six_minutes():
    from src.agents import presentation

    f = {"nav": 10_300_000, "ret": 3.0, "taiex_ret": 1.0, "trades": 40, "symbols": 9, "min_ratio": 0.75,
         "params": {"max_positions": 5, "min_hold_days": 10, "expiry_days": 40},
         "backtest": {"from": "2002-05-03", "to": "2026-10-14", "open_cagr": 0.24, "close_cagr": 0.47, "taiex_cagr": 0.09,
                      "calibration": {"buckets": [{"rank": "1", "win": 0.50}], "top5_portfolio_win": 0.56}},
         "agents": [{"name": "x", "title": "x", "llm": False}] * 12,
         "stress": [{"scenario": "加權指數 −5%", "total": -1_000_000, "pct_nav": -9.7}],
         "margin": {"pct_to_call": 4.0}, "contrib": [{"name": "A", "pnl": 100}, {"name": "B", "pnl": -50}],
         "shadow": {"diff": -1000, "breakdown": [{"name": "期貨部位（影子沒有期貨）", "amount": -500}]}}
    sc = presentation.script(f)
    assert sum(x["seconds"] for x in sc) <= presentation.LIMIT_SEC
    joined = "".join(x["text"] for x in sc)
    assert "+3.00%" in joined and "最低 75.0%" in joined and "50%" in joined
    f["min_ratio"] = 0.65
    assert "最低曾到 65.0%" in "".join(x["text"] for x in presentation.script(f))  # 有違規就不能說「都在 70% 以上」
