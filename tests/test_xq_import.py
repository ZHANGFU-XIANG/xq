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


# ---------------------------------------------------------------- 截圖（不呼叫模型，只測辨識後的驗證與轉換）
def test_screenshot_rows_flag_unknown_and_name_mismatch():
    from src.agents import xq_vision

    t = xq_vision.Table(kind="holdings", rows=[
        xq_vision.Row(code="2330", name="台積電", qty=1000, unit="股", price=1015),
        xq_vision.Row(code="2331", name="台積電", qty=1000, unit="股", price=1015),  # 代號讀錯一碼
        xq_vision.Row(code="9999", name="不存在", qty=1000, unit="股", price=10)])
    rows, warns = xq_vision.check_rows(t)
    assert [r["code"] for r in rows] == ["2330", "2331"]
    assert any("2331" in w and "核對" in w for w in warns) and any("9999" in w for w in warns)


def test_to_shares_uses_board_over_misread_amount():
    """真實 XQ 成交回報：整股的數量是張、零股是股；模型讀錯價金時不可推翻盤別。"""
    from src.agents.xq_vision import Row, to_shares

    assert to_shares(Row(code="3591", qty=20, board="整股", price=28.633, amount=572650)) == (20000, None)
    assert to_shares(Row(code="2915", qty=345, unit="股", board="盤中零股", price=57.397, amount=19802)) == (345, None)
    sh, w = to_shares(Row(code="6213", qty=1, unit="股", board="整股", price=726, amount=726))  # 模型自己補了「股」、價金讀錯
    assert sh == 1000 and "核對" in w
    assert to_shares(Row(code="2330", qty=3, price=1000, amount=3_000_000)) == (3000, None)  # 沒盤別 → 用價金
    assert to_shares(Row(code="2330", qty=1500, qty_col="庫存股數", price=1000)) == (1500, None)


def test_fills_each_row_is_a_trade_and_dedups(monkeypatch, tmp_path):
    from src.api import chat_agent
    from src.config import now_tw

    monkeypatch.setattr(pfm, "TRADES_CSV", tmp_path / "t.csv")
    today = now_tw().date().isoformat()
    pfm.append_trades([{"date": today, "code": "2330", "side": "BUY", "shares": 1000, "price": 1015, "margin": 0}])
    row = lambda code, side, sh, px, st="全部成交": {"code": code, "name": code, "side": side, "shares": sh, "filled": sh,  # noqa: E731
                                                  "price": px, "margin": False, "status": st, "board": None}
    out = chat_agent._fills_to_trades([row("2330", "BUY", 1000, 1015.0),            # 已記錄過
                                       row("0050", "BUY", 1000, 185.0), row("0050", "BUY", 200, 185.5),  # 整股＋零股分開記
                                       row("2603", "BUY", 1000, 240.0, "已刪單")])  # 略過
    assert out["duplicates"] and [(t["shares"], t["price"]) for t in out["trades"]] == [(1000, 185.0), (200, 185.5)]
    assert out["skipped"] and out["pending_action"]


def test_csv_unit_by_amount():
    """Fill-report CSVs whose 整股 quantity is already in shares (10/07 bug: 10 張 recorded as 10,000,000 股): infer the unit from 成交價金."""
    from src.agents import xq_csv

    hdr = "成交時間,委託書號,盤別,交易類別,股票名稱,成交,成交單價,成交價金\n"
    lots = hdr + "09:01:02,0aneC,整股,現股買進,合庫金(5880),10,26.15,261500\n"
    shares = hdr + "09:01:02,0aneC,整股,現股買進,合庫金(5880),\"10,000\",26.15,261500\n"
    odd = hdr + "09:20:00,0aneR,盤中零股,現股買進,合庫金(5880),251,26.15,6564\n"
    for text, want in ((lots, 10000), (shares, 10000), (odd, 251)):
        r = xq_csv.parse(text.encode("cp950"))
        assert r["rows"][0]["shares"] == want, (text, r)


def test_csv_unit_without_amount_guard():
    """No 成交價金 column: 整股 quantity of 10000 cannot be in lots (it would exceed the 10M capital), so treat it as shares."""
    from src.agents import xq_csv

    hdr = "委託時間,委託書號,盤別,交易類別,股票名稱,原委託,價格,成交,成交均價,交易日期,狀態\n"
    a = hdr + "09:01:02,0aneC,整股,現股買進,合庫金(5880),10,26.15,10,26.15,2026/10/05,全部成交\n"
    b = hdr + "09:01:02,0aneC,整股,現股買進,合庫金(5880),10000,26.15,10000,26.15,2026/10/05,全部成交\n"
    assert xq_csv.parse(a.encode("cp950"))["rows"][0]["shares"] == 10000
    assert xq_csv.parse(b.encode("cp950"))["rows"][0]["shares"] == 10000
