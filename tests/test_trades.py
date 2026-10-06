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
