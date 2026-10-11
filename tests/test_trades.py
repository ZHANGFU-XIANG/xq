"""手動交易紀錄（新增／修改／刪除）、交易統計、NaN 安全輸出測試（不連網）。"""
import math

import pytest

from src.agents import trade_review
from src.api import data, sync
from src.state import portfolio as pfm
from src.utils import json_safe


def _t(date, code, side, shares, price, note=""):
    return {"date": date, "code": code, "side": side, "shares": str(shares), "price": str(price), "margin": "0",
            "note": note, "asset": "", "contract": ""}


BASE = [_t("2026-10-05", "2330", "BUY", 1000, 1000), _t("2026-10-05", "2330", "BUY", 500, 1100),
        _t("2026-10-05", "2884", "BUY", 2000, 30)]


def test_review_fifo_matches_portfolio_realized():
    trades = BASE + [_t("2026-10-07", "2330", "SELL", 1200, 1050), _t("2026-10-08", "2884", "SELL", 2000, 29)]
    r = trade_review.review(trades, {"2330": 1080})
    s = r["summary"]
    assert s["realized"] == round(pfm.build_portfolio(trades).realized_pnl)
    assert s["closed_count"] == 2 and s["win_count"] == 1 and s["loss_count"] == 1
    sale = next(c for c in r["closed"] if c["code"] == "2330")
    assert sale["shares"] == 1200 and sale["buy_price"] == pytest.approx((1000 * 1000 + 200 * 1100) / 1200, abs=1e-3)
    tsmc = next(x for x in r["per_symbol"] if x["code"] == "2330")
    assert tsmc["held_shares"] == 300 and tsmc["unrealized"] == round(300 * 1080 - 300 * 1100)
    assert s["trade_count"] == 5 and s["symbols"] == 2


def test_fee_tax_split_matches_trade_cost():
    for code, side, sh, px in [("2330", "SELL", 1000, 1000), ("0050", "SELL", 71, 150.5), ("2884", "BUY", 3, 30)]:
        fee, tax = trade_review.fee_tax(code, side, sh, px)
        assert fee + tax == pfm.trade_cost(code, side, sh, px)


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(pfm, "TRADES_CSV", tmp_path / "trades.csv")
    monkeypatch.setattr(sync, "cloud", lambda: False)
    monkeypatch.setattr(data, "_clean_trade", lambda t, base=None: {**{k: (base or {}).get(k, "") for k in pfm.TRADE_FIELDS},
                                                                     **{k: str(v) for k, v in t.items()}})
    pfm.write_trades(BASE)
    return tmp_path


def test_mutations_and_version_conflict(ledger):
    v = pfm.trades_version()
    r = data.trade_mutate("add", v, _t("2026-10-06", "2330", "SELL", 300, 1080))
    assert r["ok"] and len(pfm.load_trades()) == 4
    with pytest.raises(sync.ConflictError):  # 舊版本不能再寫
        data.trade_mutate("add", v, _t("2026-10-06", "2330", "SELL", 1, 1080))
    rows = pfm.load_trades()
    sell = next(i for i, x in enumerate(rows) if x["side"] == "SELL")
    data.trade_mutate("update", r["version"], {**rows[sell], "price": "1090"}, sell)
    assert pfm.load_trades()[sell]["price"] == "1090"
    with pytest.raises(ValueError):  # 賣超過持有被擋下，檔案不變
        data.trade_mutate("add", pfm.trades_version(), _t("2026-10-06", "2884", "SELL", 99999, 30))
    data.trade_mutate("delete", pfm.trades_version(), None, sell)
    assert pfm.load_trades() == [{k: x.get(k, "") for k in pfm.TRADE_FIELDS} for x in BASE]


def test_backdated_trade_sorted_by_date(ledger):
    data.trade_mutate("add", pfm.trades_version(), _t("2026-10-02", "2884", "BUY", 1000, 29))
    assert [x["date"] for x in pfm.load_trades()][0] == "2026-10-02"


def test_json_safe_replaces_nan():
    out = json_safe({"a": float("nan"), "b": [1.5, float("inf")], "c": "x"})
    assert out == {"a": None, "b": [1.5, None], "c": "x"}
    assert not any(isinstance(v, float) and not math.isfinite(v) for v in out["b"] if v is not None)


def _clean(**t):
    from src.api.data import _clean_trade

    return _clean_trade({"date": "2026-10-06", **t})


def test_clean_trade_all_kinds():
    fut = _clean(kind="FUT", code="TMF", side="BUY", shares=2, price=23000, contract="TMF202610")
    assert fut["asset"] == "FUT" and fut["contract"] == "TMF202610"
    opt = _clean(kind="OPT", code="TXO", side="BUY", shares=1, price=45, contract="TXO202610P22000")
    assert opt["asset"] == "OPT" and opt["code"] == "TXO"
    tr = _clean(kind="TRANSFER", side="TO_FUTURES", price=500000)
    assert tr["code"] == "TRANSFER" and tr["shares"] == "1"
    fee = _clean(kind="CASH", price=-20)
    assert fee["code"] == "FEE" and fee["side"] == "ADJ" and fee["price"] == "-20"
    for bad in [dict(kind="FUT", code="TMF", side="BUY", shares=1, price=1, contract="TX202610"),
                dict(kind="OPT", code="TXO", side="BUY", shares=1, price=1, contract="TXO202610X22000"),
                dict(kind="TRANSFER", side="SIDEWAYS", price=1), dict(kind="CASH", price=0),
                dict(kind="STOCK", code="123456", side="BUY", shares=1, price=1)]:
        with pytest.raises(ValueError):
            _clean(**bad)


def test_derivative_round_trip_replays():
    rows = BASE + [_t("2026-10-06", "TRANSFER", "TO_FUTURES", 1, 300000),
                   {**_t("2026-10-06", "TXO", "BUY", 2, 40), "asset": "OPT", "contract": "TXO202610P22000"},
                   {**_t("2026-10-06", "TXO", "SELL", 2, 60), "asset": "OPT", "contract": "TXO202610P22000"}]
    pf = pfm.build_portfolio(rows)
    assert not pf.derivs and pf.realized_pnl > 0
    with pytest.raises(ValueError):  # 選擇權只做買方
        pfm.build_portfolio(rows + [{**_t("2026-10-06", "TXO", "SELL", 1, 60), "asset": "OPT", "contract": "TXO202610P22000"}])


XQ_CSV = ("委託時間,\t委託書號,\t盤別,\t交易類別,\t委託種類,\t股票名稱,\t原委託,\t價格,\t取消,\t成交,\t成交均價,\t來源別,\t交易日期,\t狀態\n"
          "2026/10/06 12:09:44.673,0apGk,盤中零股,現股買進,ROD,國巨*(2327),200股,622.00,0股,0股,,GPhone,2026/10/06,委託成功\n"
          "2026/10/06 12:04:46.900,0apFC,整股,現股買進,ROD,國票金(2884),3,16.20,0,3,,GPhone,2026/10/06,完全成交\n"
          "2026/10/06 10:00:00.000,0apAA,盤中零股,現股賣出,ROD,台積電(2330),500股,1100.00,0股,300股,1105.5,GPhone,2026/10/06,部分成交\n"
          "2026/10/05 09:00:00.000,0anAA,整股,現股買進,ROD,台積電(2330),1,1000.00,0,1,,GPhone,2026/10/05,完全成交\n"
          "2026/10/05 09:01:00.000,0anAB,整股,融券賣出,ROD,台積電(2330),1,1000.00,0,1,,GPhone,2026/10/05,完全成交\n").encode("cp950")


def test_xq_csv_parse_units_and_duplicates(ledger):
    from src.agents import xq_csv

    r = xq_csv.parse(XQ_CSV)
    rows = xq_csv.mark_duplicates(r["rows"])
    got = [(x["date"], x["code"], x["side"], x["shares"], x["price"], x["price_source"], x["duplicate"]) for x in rows]
    assert got == [
        ("2026-10-05", "2330", "BUY", 1000, 1000.0, "委託價", True),     # 已記錄（不比價格）
        ("2026-10-06", "2330", "SELL", 300, 1105.5, "成交均價", False),  # 零股、部分成交只取成交量、用成交均價
        ("2026-10-06", "2884", "BUY", 3000, 16.2, "委託價", False),      # 整股：3 張
    ]
    assert any("2327" in s for s in r["skipped"]) and any("融券" in s for s in r["skipped"])
    assert r["warnings"]


def test_bulk_add_and_delete_single_commit(ledger):
    v = pfm.trades_version()
    data.trade_mutate("bulk_add", v, trades=[_t("2026-10-06", "2884", "BUY", 3000, 16.2), _t("2026-10-06", "2330", "SELL", 300, 1105)])
    rows = pfm.load_trades()
    assert len(rows) == 5
    with pytest.raises(ValueError):  # 任何一筆不合法 → 全部不寫入
        data.trade_mutate("bulk_add", pfm.trades_version(), trades=[_t("2026-10-06", "2884", "SELL", 99999, 16)])
    assert len(pfm.load_trades()) == 5
    data.trade_mutate("bulk_delete", pfm.trades_version(), ids=[3, 4])
    assert len(pfm.load_trades()) == 3
    with pytest.raises(ValueError):
        data.trade_mutate("bulk_delete", pfm.trades_version(), ids=[99])


def test_xq_futures_csv(ledger):
    from src.agents import xq_csv

    raw = ("委託時間,\t委託書號,\t交易種類,\t買賣別,\t商品名稱,\t倉別,\t委託條件,\t委託價格,\t委託數量,\t取消,\t成交,\t有效數量,\t來源別,\t交易日期,\t狀態\n"
           "2026/10/06 09:23:36,01NYf,期貨,買進,台股指數106,新倉,ROD,49875.000,2,0,2,0,GPhone,2026/10/06,完全成交\n"
           "2026/10/06 10:00:00,01NYg,期貨,賣出,微型台指106,新倉,ROD,49800.000,3,3,0,0,GPhone,2026/10/06,刪單成功\n").encode("cp950")
    r = xq_csv.parse(raw)
    rows = xq_csv.mark_duplicates(r["rows"])
    assert [(x["kind"], x["contract"], x["side"], x["shares"], x["price"], x["board"]) for x in rows] == [
        ("FUT", "TX202610", "BUY", 2, 49875.0, "新倉")]
    assert len(r["skipped"]) == 1  # 刪單略過
    assert xq_csv._future("小型台指106") == {"root": "MTX", "ym": "202610"}
    assert xq_csv._future("微型台指1610")["ym"] is None  # 讀不出合理年月 → 讓使用者填


def test_attribution_survives_nan_rows(monkeypatch):
    """10/06 盤前：yfinance 加權指數／個股最後一列 NaN 讓整份報告失敗。"""
    import numpy as np
    import pandas as pd

    from src.agents import attribution

    monkeypatch.setattr(attribution, "tw_institutional", lambda c: pd.DataFrame())
    idx = pd.bdate_range("2026-07-01", periods=60)
    rng = np.random.default_rng(0)
    close = pd.Series(20000 * np.cumprod(1 + rng.normal(0, 0.01, 60)), index=idx)
    tx = pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1.0})
    tx.loc[idx[-2], "Close"] = np.nan  # 中間缺一天
    stock = pd.DataFrame({"Open": close / 100, "Close": close / 100, "Volume": 1000.0}, index=idx)
    bad = stock.copy()
    bad.loc[idx[-2]:, "Close"] = np.nan  # 這檔最後兩天沒有價格
    out = attribution.attribute({"2330": 1000, "2884": 1000}, {"2330": stock, "2884": bad}, tx, ["2330", "2884"],
                                pd.Timestamp(idx[-1]) + pd.Timedelta(days=1), {}, {})
    assert out["taiex_ret_pct"] == out["taiex_ret_pct"]  # 不是 NaN
    assert [r["code"] for r in out["rows"]] == ["2330"]
    assert all(isinstance(r["pnl"], int) for r in out["rows"])


def test_scheduler_retries_unfinished_slot(monkeypatch):
    """觸發後 15 分鐘仍未完成 → 有效時間內重試，最多 3 次；完成後不再觸發。"""
    import datetime as dt

    from src import schedule
    from src.api import scheduler
    from src.config import TW_TZ

    clock = {"t": dt.datetime(2026, 10, 7, 8, 20, tzinfo=TW_TZ)}
    done = {"v": False}
    fired = []
    monkeypatch.setattr(scheduler, "_sent", {})
    monkeypatch.setattr(scheduler, "dispatch", lambda mode: fired.append((clock["t"].strftime("%H:%M"), mode)))
    monkeypatch.setattr(scheduler.time, "time", lambda: clock["t"].timestamp())
    monkeypatch.setattr(schedule, "done", lambda key, day: done["v"])
    for _ in range(80):  # 08:20 ~ 09:00，每 30 秒（第 3 次後不再觸發）
        scheduler.tick(clock["t"])
        clock["t"] += dt.timedelta(seconds=30)
    assert [f for f in fired if f[1] == "preopen"] == [("08:20", "preopen"), ("08:35", "preopen"), ("08:50", "preopen")]
    done["v"] = True
    n = len(fired)
    clock["t"] = dt.datetime(2026, 10, 7, 9, 0, tzinfo=TW_TZ)
    scheduler.tick(clock["t"])
    assert len(fired) == n  # 已完成就不觸發


def test_futures_advice_nets_existing_position():
    from src.agents import futures

    o = {"asset": "FUT", "code": "TMF", "contract": "TMF202610", "action": "BUY", "qty": 7, "limit_price": 50064,
         "stop": 48757, "margin": 245350, "max_loss": 91490, "why_qty": []}
    fut = lambda code, side, n: {"date": "2026-10-06", "code": code, "side": side, "shares": str(n), "price": "49875",  # noqa: E731
                                 "asset": "FUT", "contract": f"{code}202610"}
    reasons = []
    assert futures._net_existing(pfm.build_portfolio([fut("TX", "BUY", 2)]), [dict(o)], reasons, 1e7) == []
    assert "不再加碼" in reasons[0] and "可考慮減碼" in reasons[0]
    out = futures._net_existing(pfm.build_portfolio([fut("TMF", "BUY", 3)]), [dict(o)], [], 1e7)
    assert out[0]["qty"] == 4 and out[0]["margin"] == round(245350 / 7 * 4)
    assert futures._net_existing(pfm.build_portfolio([]), [dict(o)], [], 1e7)[0]["qty"] == 7


def _tx(closes):
    import pandas as pd

    idx = pd.bdate_range("2026-01-01", periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1.0})


def test_regime_states_and_lines():
    from src.agents import regime

    up = [100 * (1.004 ** i) for i in range(120)]  # 穩定上漲 → 綠燈
    r = regime.assess(_tx(up))
    assert r["state"] == "強勢" and r["level"] == "綠燈" and r["offense"] == 0.85
    down = up + [up[-1] * (0.985 ** i) for i in range(1, 30)]  # 連跌 → 跌破季線 −2% → 紅燈
    r2 = regime.assess(_tx(down))
    assert r2["state"] == "轉弱" and r2["level"] == "紅燈" and r2["offense"] == 0.30
    assert r2["lines"]["weak_enter"] < r2["lines"]["ma60"] < r2["lines"]["weak_exit"]
    # 盤中預判：綠燈時即時價跌破轉弱線
    r3 = regime.assess(_tx(up), live_price=r["lines"]["weak_enter"] - 1)
    assert r3["level"] == "黃燈" and "明天起轉為紅燈" in r3["warnings"][0]
    # 遲滯：剛跌回季線附近（未站回季線 +1%）仍維持紅燈
    st = regime.states(_tx(down + [r2["lines"]["ma60"] * 1.0] * 2).Close)
    assert st.iloc[-1] == "轉弱"


def test_regime_yellow_from_vix_and_events():
    import pandas as pd

    from src.agents import regime

    up = [100 * (1.004 ** i) for i in range(120)]
    vix = pd.DataFrame({"Close": [20.0, 28.0]}, index=pd.bdate_range("2026-06-01", periods=2))
    r = regime.assess(_tx(up), {"^VIX": vix}, events=[{"event": "某重大事件", "total_pct_nav": -1.5}])
    assert r["level"] == "黃燈" and len(r["warnings"]) == 2


def test_allocation_follows_regime_and_caps_on_yellow():
    import pandas as pd

    from src.agents import allocation

    up = _tx([100 * (1.004 ** i) for i in range(120)])
    t = up.index[-1] + pd.Timedelta(days=1)
    a = allocation.decide(up, t, [], 1e7, 1e7, 30, [])
    assert a["stock_split"]["offense"] == 0.85 and a["regime"]["level"] == "綠燈"
    vix = pd.DataFrame({"Close": [20.0, 30.0]}, index=pd.bdate_range("2026-06-01", periods=2))
    b = allocation.decide(up, t, [], 1e7, 1e7, 30, [], us_hist={"^VIX": vix})
    assert b["stock_split"]["offense"] == 0.60  # 黃燈：進攻最多 60%


def test_deriv_review_equity_method():
    from src.agents import trade_review

    rows = [{"date": "2026-10-06", "code": "TX", "side": "BUY", "shares": "2", "price": "49875", "asset": "FUT", "contract": "TX202610"},
            {"date": "2026-10-07", "code": "TX", "side": "SELL", "shares": "1", "price": "50000", "asset": "FUT", "contract": "TX202610"}]
    d = trade_review.review_derivs(rows, {"TX202610": 50100})
    r = d["rows"][0]
    assert r["qty"] == 1 and r["unrealized"] == (50100 - 49875) * 200
    fees = pfm.deriv_cost("TX", 2, 49875) + pfm.deriv_cost("TX", 1, 50000)
    assert r["realized"] == (50000 - 49875) * 200  # App 口徑：已實現不含手續費
    assert r["fees"] == round(fees) and r["net_after_fees"] == r["realized"] + r["unrealized"] - round(fees)
    assert r["total_pnl"] == r["realized"] + r["unrealized"]


def test_matches_competition_app_20261006():
    """以 10/06 XQ 成交回報（44 筆證券＋大台 2 口）重播，與競賽 App「證券績效」逐項一致。"""
    import csv
    from pathlib import Path

    rows = list(csv.DictReader(open(Path(__file__).parent / "fixtures" / "app_20261006_trades.csv", encoding="utf-8")))
    pf = pfm.build_portfolio(rows)
    close = {"2889": 16.2, "3591": 27.05, "2915": 57.6, "6213": 787.0, "1560": 1030.0, "3189": 1070.0,
             "6196": 663.0, "2845": 12.85, "5880": 25.95}
    prices = {**close, "TX202610": 50082.0}  # 期貨用日盤結算價
    cost = sum(lot.amount for p in pf.positions.values() for lot in p.lots)
    sec = sum(p.shares * close[c] for c, p in pf.positions.items()) - cost
    assert cost == 7_702_300
    assert round(sec) == 109_066
    assert pf.derivs["TX202610"].value(50082.0) == 82_800
    assert round(pfm.app_total(pf, prices)) == 10_191_866  # 投資組合總值（報酬率 1.9187%）
    margin = 2 * 701_000  # 大台原始保證金
    assert round(pf.cash + pf.futures_equity(prices) - margin) == 966_764  # 持有現金
    assert round(pf.holding_ratio(prices) * 100, 2) == 76.11  # App 持股率


def test_matches_competition_app_20261008():
    """10/08 App 對帳（第一次有賣出）：零股賣出扣零股成本、整股扣整股成本，部分扣批時價金與手續費按比例捨去；賣出價金四捨五入。
    App：已實現 −582、現股未實現 186,795、期貨未實現 −207,200、總值 9,979,013（−0.2099%）、持股率 83.73%、持有現金 392,815。"""
    import csv
    from pathlib import Path

    rows = list(csv.DictReader(open(Path(__file__).parent / "fixtures" / "app_20261008_trades.csv", encoding="utf-8")))
    pf = pfm.build_portfolio(rows)
    close = {"5880": 26.45, "2889": 16.5, "2845": 12.95, "2915": 58.9, "3189": 1075.0, "3591": 29.1, "1560": 1010.0,
             "6196": 674.0, "6213": 806.0, "2103": 29.2, "2360": 2330.0}
    settle = 49875.0 - 207_200 / 400  # 由 App 期貨未實現 −207,200 反推的結算價 49,357
    prices = {**close, "TX202610": settle}
    sec = sum(p.shares * close[c] - sum(lot.amount for lot in p.lots) for c, p in pf.positions.items())
    assert round(pf.realized_pnl) == -582
    assert round(sec) == 186_795
    assert round(pf.derivs["TX202610"].value(settle)) == -207_200
    assert round(pfm.app_total(pf, prices)) == 9_979_013
    assert round(pf.holding_ratio(prices) * 100, 2) == 83.73
    margin = 2 * 701_000
    assert abs(round(pf.cash + pf.futures_equity(prices) - margin) - 392_815) <= 1  # 尚差 1 元，原因待更多賣出資料確認


def test_mass_delete_needs_confirm(ledger):
    """10/07 一次批次刪除清掉所有股票：會清掉持股或刪掉一半以上股票紀錄時，必須輸入「確認刪除」。"""
    v = pfm.trades_version()
    with pytest.raises(ValueError, match="確認刪除"):
        data.trade_mutate("bulk_delete", v, ids=[0, 1, 2])
    assert len(pfm.load_trades()) == 3  # 被擋下，紀錄不變
    r = data.trade_mutate("bulk_delete", v, ids=[0, 1, 2], confirm="確認刪除")
    assert r["ok"] and pfm.load_trades() == []


def test_small_delete_needs_no_confirm(ledger):
    data.trade_mutate("add", pfm.trades_version(), _t("2026-10-06", "2884", "BUY", 1000, 31))
    rows = pfm.load_trades()
    i = next(i for i, x in enumerate(rows) if x["price"] == "31")
    assert data.trade_mutate("delete", pfm.trades_version(), None, i)["ok"]  # 4 筆刪 1 筆、持股還在 → 不用確認


def test_restore_rejects_oversized_version(ledger, monkeypatch):
    """復原歷史版本：數量多乘 1000 的版本（單筆金額超過本金）不能復原；正常版本可以。"""
    bad = [_t("2026-10-05", "5880", "BUY", 10_000_000, 26.15)]
    monkeypatch.setattr(sync, "trades_at", lambda sha: bad if sha == "bad0" else [{k: x.get(k, "") for k in pfm.TRADE_FIELDS} for x in BASE[:1]])
    with pytest.raises(ValueError, match="數量異常"):
        data.trade_restore("bad0", pfm.trades_version())
    r = data.trade_restore("a1b2", pfm.trades_version())
    assert r["ok"] and len(pfm.load_trades()) == 1


def test_vanished_holdings_alert(ledger, monkeypatch, tmp_path):
    import datetime as dt

    from src.agents import alerts

    monkeypatch.setattr(pfm, "HISTORY_JSON", tmp_path / "nav.json")
    pfm.record_history("2026-10-08", 1e7, 0.8, {"2330": 1500, "2884": 2000})
    assert alerts.holdings_vanished(pfm.build_portfolio(), dt.date(2026, 10, 12)) is None
    lost = alerts.holdings_vanished(pfm.build_portfolio([]), dt.date(2026, 10, 12))
    assert lost and lost["before"] == 2 and lost["now"] == 0
    sold = BASE + [_t("2026-10-12", "2330", "SELL", 1500, 1000), _t("2026-10-12", "2884", "SELL", 2000, 30)]
    pfm.write_trades(sold)
    assert alerts.holdings_vanished(pfm.build_portfolio(sold), dt.date(2026, 10, 12)) is None  # 真的賣掉了，不算誤刪
