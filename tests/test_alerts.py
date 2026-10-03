"""警示規則：觸發條件 → 等級與建議賣出股數（不連網）。"""
from src.agents import alerts
from src.state.portfolio import build_portfolio


def _pf(shares=3000, price=100):
    return build_portfolio([{"date": "d", "code": "2303", "side": "BUY", "shares": str(shares), "price": str(price), "margin": "0"}])


FEAT = {"close": 100, "ret_1d": 0.0, "ma20": 95, "atr14": 3.0, "inst_buy_streak": 1, "inst_net5_ratio": 0.0, "inst_net5": 0}


def _run(rt=None, risks=None, stop=90, att=None, feat=None):
    return alerts.holding_alerts(_pf(), {"2303": {"stop": stop}}, {"2303": rt} if rt else {}, {"2303": 100},
                                 {"2303": feat or FEAT}, att or {}, {"2303": risks} if risks else {})


def test_stop_break_sells_all():
    a = _run(rt={"price": 89, "prev_close": 92, "chg_pct": -3.3, "limit_down": 82.8, "at_limit_down": False})[0]
    assert a["level"] == "嚴重" and a["sell_shares"] == 3000 and a["limit_price"] >= 82.8


def test_limit_down_sells_half_and_respects_limit_price():
    a = _run(rt={"price": 90, "prev_close": 100, "chg_pct": -10, "limit_down": 90, "at_limit_down": True}, stop=80)[0]
    assert a["level"] == "嚴重" and a["sell_shares"] == 1500 and a["limit_price"] == 90


def test_major_negative_news_above_ma_halves():
    r = [{"severity": -2, "issue": "大客戶流失", "multi_source": True, "news": []}]
    a = _run(risks=r)[0]
    assert a["level"] == "嚴重" and a["sell_shares"] == 1500


def test_major_negative_news_below_ma_sells_all():
    r = [{"severity": -2, "issue": "財報下修", "multi_source": False, "news": []}]
    a = _run(risks=r, feat={**FEAT, "ma20": 110})[0]
    assert a["sell_shares"] == 3000


def test_minor_negative_news_strong_price_tightens_stop_only():
    r = [{"severity": -1, "issue": "調降目標價", "multi_source": False, "news": []}]
    a = _run(risks=r)[0]
    assert a["level"] == "警告" and a["sell_shares"] == 0 and a["new_stop"] == 97.0


def test_disposal_reduces_one_third():
    a = _run(att={"2303": {"type": "處置", "reason": "連續三次", "period": "10/01~10/07"}})[0]
    assert a["level"] == "警告" and a["sell_shares"] == 1000


def test_no_alert_when_nothing_triggers():
    assert _run() == []


def test_buy_order_with_major_news_is_paused():
    orders = [{"action": "BUY", "code": "2892", "name": "第一金", "shares": 10000}]
    out = alerts.order_alerts(orders, {}, {"2892": [{"severity": -2, "issue": "弊案", "multi_source": True, "news": []}]}, {})
    assert out[0]["level"] == "嚴重" and "暫緩" in out[0]["action"]


def test_limit_moves_grouping():
    q = [{"code": "2330", "name": "台積電", "chg_pct": 9.9, "value": 10}, {"code": "2303", "name": "聯電", "chg_pct": 9.8, "value": 5},
         {"code": "2603", "name": "長榮", "chg_pct": -9.9, "value": 3}, {"code": "2317", "name": "鴻海", "chg_pct": 3, "value": 9}]
    lim = alerts.limit_moves(q)
    assert [x["code"] for x in lim["up"]] == ["2330", "2303"] and lim["down"][0]["code"] == "2603"


def test_rule_alerts_trades_and_symbols():
    import datetime as dt

    a = alerts.rule_alerts(_pf(), [{"code": "2330", "action": "BUY"}], dt.date(2026, 10, 6))
    t = {x["title"].split(" ")[0]: x for x in a}
    assert "還差 9 筆" in a[0]["title"] and a[0]["scope"] == "規則"
    assert any("還差 4 檔" in x["title"] and x["level"] == "警告" for x in a)  # 建倉期只警告
    late = alerts.rule_alerts(_pf(), [], dt.date(2026, 11, 23))
    assert all(x["level"] == "嚴重" for x in late)
    assert t
