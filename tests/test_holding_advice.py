"""持股建議：訂單與警示 → 加碼／續抱／減碼／全部出清。"""
from src.agents import holding_advice
from src.state.portfolio import build_portfolio


def _pf():
    return build_portfolio([
        {"date": "d", "code": "2330", "side": "BUY", "shares": "1000", "price": "1000", "margin": "0"},
        {"date": "d", "code": "2303", "side": "BUY", "shares": "3000", "price": "50", "margin": "0"},
        {"date": "d", "code": "2603", "side": "BUY", "shares": "2000", "price": "200", "margin": "0"},
        {"date": "d", "code": "2892", "side": "BUY", "shares": "5000", "price": "30", "margin": "0"}])


PRICES = {"2330": 1050, "2303": 48, "2603": 180, "2892": 31}
META = {"2330": {"stop": 900}, "2303": {"stop": 45}, "2603": {"stop": 185}, "2892": {"stop": 28, "basket": "defense"}}
BO = {"2330": "offense", "2303": "offense", "2603": "offense", "2892": "defense"}


def _orders():
    return [
        {"action": "SELL", "code": "2603", "shares": 2000, "limit_price": 178, "why_sell": "觸發停損", "why_qty": "全數出場"},
        {"action": "SELL", "code": "2303", "shares": 1000, "limit_price": 47.5, "why_sell": "訊號轉弱", "why_qty": "減碼"},
        {"action": "BUY", "code": "2330", "shares": 500, "limit_price": 1060, "why_stock": ["分數高"], "why_qty": ["取整後：500 股"], "stop": 980},
    ]


def test_verdicts_from_orders():
    items = {i["code"]: i for i in holding_advice.build(_pf(), PRICES, {}, {}, META, _orders(), [], BO, 10_000_000)}
    assert items["2603"]["verdict"] == "全部出清" and items["2603"]["qty"] == 2000
    assert items["2303"]["verdict"] == "減碼" and "保留 2 張" in items["2303"]["qty_text"]
    assert items["2330"]["verdict"] == "加碼" and items["2330"]["qty"] == 500
    assert items["2892"]["verdict"] == "續抱" and items["2892"]["qty"] == 0
    order = [i["verdict"] for i in holding_advice.build(_pf(), PRICES, {}, {}, META, _orders(), [], BO, 10_000_000)]
    assert order == ["全部出清", "減碼", "加碼", "續抱"]


def test_add_on_take_profit_uses_new_average_cost(monkeypatch):
    # 2026-10-12 起預設不分段停利；這裡明確設定停利段數，驗證「設定時」停利價用加碼後成本
    from src.state import params

    orig = params.basket
    monkeypatch.setattr(params, "basket", lambda name: {**orig(name), "take_profit": [[0.10, 0.3333], [0.20, 0.5]]})
    items = {i["code"]: i for i in holding_advice.build(_pf(), PRICES, {}, {}, META, _orders(), [], BO, 10_000_000)}
    tp = next(c for c in items["2330"]["conditions"] if "停利" in c)
    assert "加碼後成本" in tp  # 平均成本約 1020 → +10% 約 1120，不會低於加碼價 1060
    price = float(tp.split("漲到 ")[1].split("（")[0])
    assert price > 1060


def test_alert_sell_overrides_and_cancels_add_on():
    alerts = [{"scope": "持股", "code": "2330", "sell_shares": 1000, "limit_price": 1040, "price": 1050,
               "reasons": ["重大負面新聞：財報下修"], "why_qty": "全數出場"}]
    merged = holding_advice.merge_alert_sells(_orders(), alerts, _pf(), PRICES, BO)
    mine = [o for o in merged if o["code"] == "2330"]
    assert len(mine) == 1 and mine[0]["action"] == "SELL" and mine[0]["shares"] == 1000
    items = {i["code"]: i for i in holding_advice.build(_pf(), PRICES, {}, {}, META, merged, alerts, BO, 10_000_000)}
    assert items["2330"]["verdict"] == "全部出清"


def test_alert_smaller_than_existing_sell_keeps_order():
    alerts = [{"scope": "持股", "code": "2603", "sell_shares": 500, "reasons": ["注意股"]}]
    merged = holding_advice.merge_alert_sells(_orders(), alerts, _pf(), PRICES, BO)
    assert next(o for o in merged if o["code"] == "2603")["shares"] == 2000
