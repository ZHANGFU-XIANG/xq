"""盤中建議：計畫訂單 vs 即時價的判斷規則。"""
from src.agents.intraday import _order_status, current_slot

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
    # 盤中計畫是用目前持股重算的，訂單股數就是「還要買的量」
    assert _order_status(BUY, rt(998), 0)["remaining"] == 1500


def test_sell_rules():
    assert _order_status(SELL, rt(900, down=True), 1000)["status"] == "跌停賣不掉"
    assert _order_status(SELL, rt(995), 0)["status"] == "已成交"
    assert _order_status(SELL, rt(995), 1000)["status"] == "可以下單"


def test_slots_every_40_minutes():
    import datetime as dt

    from src.config import TW_TZ

    def at(h, m):
        return current_slot(dt.datetime(2026, 10, 5, h, m, tzinfo=TW_TZ))[-5:]

    assert at(8, 50).endswith("盤前") and at(9, 0) == "09:00" and at(9, 39) == "09:00" and at(9, 40) == "09:40"
    assert at(10, 25) == "10:20" and at(12, 59) == "12:20" and at(13, 10) == "13:00"
    assert at(14, 0).endswith("收盤後")
    assert current_slot(dt.datetime(2026, 10, 3, 10, 0, tzinfo=TW_TZ)).endswith("休市")  # 週六


def test_compare_with_morning_plan():
    from src.agents.intraday import _compare

    morning = [{"code": "2330", "name": "台積電", "action": "BUY", "shares": 1000},
               {"code": "2603", "name": "長榮", "action": "BUY", "shares": 5000}]
    plan = [{"code": "2330", "name": "台積電", "action": "BUY", "shares": 1500},
            {"code": "2892", "name": "第一金", "action": "BUY", "shares": 10000}]
    ch = {c["code"]: c for c in _compare(plan, morning, [{"code": "2603", "reason": "盤中已漲 +8.00%，不追價"}])}
    assert ch["2330"]["change"] == "數量調整" and ch["2892"]["change"] == "新增"
    assert ch["2603"]["change"] == "取消" and "不追價" in ch["2603"]["detail"]


def test_premarket_orders_are_preplaced():
    r = _order_status(BUY, {}, 0, market_open=False)
    assert r["status"] == "可預掛單" and "預掛" in r["advice"] and "1 張又 500 股" in r["advice"]


def test_market_open_from_slot_labels():
    from src.agents.intraday import market_open, slot_label

    assert slot_label("2026-10-04 休市") == "休市" and not market_open("2026-10-04 休市")
    assert not market_open("2026-10-05 盤前") and not market_open("2026-10-05 收盤後")
    assert market_open("2026-10-05 09:40") and slot_label("2026-10-05 09:40") == "09:40"
