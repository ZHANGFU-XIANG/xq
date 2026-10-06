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
