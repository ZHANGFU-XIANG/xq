"""XQ 庫存貼上解析與對帳。"""
from src.agents import xq_import
from src.state import portfolio as pfm

SAMPLE_TAB = "商品代碼\t商品名稱\t類別\t庫存股數\t成本均價\t現價\t損益\n" \
             "2330\t台積電\t現股\t1,000\t2,450.00\t2,500\t50,000\n" \
             "2603\t長榮\t融資\t5,000\t240.5\t238\t-12,500\n" \
             "合計\t\t\t\t\t\t37,500\n"
SAMPLE_SPACE = "代號    名稱      張數    均價\n0050    元大台灣50    3    185.2\n"


def test_parse_tab_table():
    p = xq_import.parse(SAMPLE_TAB)
    rows = {r["code"]: r for r in p["rows"]}
    assert p["header_found"] and set(rows) == {"2330", "2603"}
    assert rows["2330"]["shares"] == 1000 and rows["2330"]["cost"] == 2450.0
    assert rows["2603"]["margin"] is True and rows["2603"]["shares"] == 5000


def test_parse_space_table_with_lots():
    r = xq_import.parse(SAMPLE_SPACE)["rows"][0]
    assert r["code"] == "0050" and r["shares"] == 3000 and r["cost"] == 185.2


def test_reconcile_proposes_adjustments(monkeypatch, tmp_path):
    monkeypatch.setattr(pfm, "TRADES_CSV", tmp_path / "t.csv")
    pfm.append_trades([{"date": "2026-10-05", "code": "2330", "side": "BUY", "shares": 500, "price": 2400, "margin": 0},
                       {"date": "2026-10-05", "code": "3037", "side": "BUY", "shares": 1000, "price": 1300, "margin": 0}])
    rec = xq_import.reconcile(xq_import.parse(SAMPLE_TAB))
    adj = {a["code"]: a for a in rec["adjustments"]}
    assert adj["2330"]["side"] == "BUY" and adj["2330"]["shares"] == 500 and adj["2330"]["price"] == 2450.0
    assert adj["2603"]["shares"] == 5000 and adj["2603"]["margin"] == 1
    assert adj["3037"]["side"] == "SELL" and adj["3037"]["shares"] == 1000  # XQ 沒有 → 視為已賣出
    assert not rec["in_sync"]
