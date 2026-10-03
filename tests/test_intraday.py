"""盤中建議：計畫訂單 vs 即時價的判斷規則。"""
from src.agents.intraday import _order_status

BUY = {"code": "2330", "name": "台積電", "action": "BUY", "basket": "offense", "shares": 1500, "limit_price": 1000.0}
SELL = {"code": "2330", "name": "台積電", "action": "SELL", "shares": 1000, "limit_price": 990.0}


def rt(price, up=False, down=False):
    return {"2330": {"price": price, "chg_pct": 0, "at_limit_up": up, "at_limit_down": down, "limit_down": 900}}


def test_buy_when_price_at_or_below_limit():
    r = _order_status(BUY, rt(998), 0)
    assert r["status"] == "可以下單" and r["remaining"] == 1500 and "1 張又 500 股" in r["advice"]


def test_do_not_chase_above_limit():
    assert _order_status(BUY, rt(1005), 0)["status"] == "接近限價"
    assert _order_status(BUY, rt(1030), 0)["status"] == "不追價"


def test_limit_up_and_filled():
    assert _order_status(BUY, rt(1100, up=True), 0)["status"] == "漲停買不到"
    assert _order_status(BUY, rt(998), 1500)["status"] == "已成交"
    partial = _order_status(BUY, rt(998), 500)
    assert partial["remaining"] == 1000


def test_sell_rules():
    assert _order_status(SELL, rt(900, down=True), 1000)["status"] == "跌停賣不掉"
    assert _order_status(SELL, rt(995), 0)["status"] == "已成交"
    assert _order_status(SELL, rt(995), 1000)["status"] == "可以下單"
