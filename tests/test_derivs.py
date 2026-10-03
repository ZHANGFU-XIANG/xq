"""期貨／選擇權部位：損益、權益數、保證金、持股率不含期貨、只做選擇權買方、資金調撥。"""
import pytest

from src.state.portfolio import build_portfolio, deriv_cost, parse_option


def t(date, code, side, qty, price, contract="", asset=""):
    return {"date": date, "code": code, "side": side, "shares": str(qty), "price": str(price), "margin": "0",
            "contract": contract, "asset": asset}


def test_futures_long_close_profit_and_costs():
    pf = build_portfolio([t("d1", "TMF", "BUY", 2, 48000, "TMF202610"), t("d2", "TMF", "SELL", 2, 48500, "TMF202610")])
    pnl = (48500 - 48000) * 10 * 2
    cost = deriv_cost("TMF", 2, 48000) + deriv_cost("TMF", 2, 48500)
    assert pf.futures_cash == pytest.approx(2_000_000 + pnl - cost)
    assert not pf.derivs and pf.trade_count == 2 and not pf.traded_symbols  # 期貨不算「交易過的股票標的」


def test_short_unrealized_and_equity_and_margin():
    pf = build_portfolio([t("d1", "MTX", "SELL", 1, 48000, "MTX202610")])
    d = pf.derivs["MTX202610"]
    assert d.qty == -1
    prices = {"MTX202610": 47500}
    assert pf.futures_equity(prices) == pytest.approx(pf.futures_cash + 500 * 50)
    assert pf.margin_required({"MTX": {"initial": 175250, "maintenance": 134500}}) == 175250


def test_reverse_position_sets_new_avg():
    pf = build_portfolio([t("d1", "TMF", "BUY", 1, 48000, "TMF202610"), t("d2", "TMF", "SELL", 3, 48200, "TMF202610")])
    d = pf.derivs["TMF202610"]
    assert d.qty == -2 and d.avg_price == 48200


def test_options_buyer_only():
    c = "TXO202610P46000"
    pf = build_portfolio([t("d1", "TXO", "BUY", 2, 100, c)])
    assert pf.futures_cash == pytest.approx(2_000_000 - 100 * 50 * 2 - deriv_cost("TXO", 2, 100))
    assert pf.futures_equity({c: 150}) == pytest.approx(pf.futures_cash + 150 * 50 * 2)
    with pytest.raises(ValueError, match="只做選擇權買方"):
        build_portfolio([t("d1", "TXO", "SELL", 1, 100, c)])
    assert parse_option(c) == {"ym": "202610", "cp": "P", "strike": 46000.0}


def test_holding_ratio_excludes_futures_and_transfer():
    rows = [{"date": "d0", "code": "TRANSFER", "side": "TO_SECURITIES", "shares": "1", "price": "1500000", "margin": "0"},
            t("d1", "2330", "BUY", 3000, 2500, asset="STOCK"),
            t("d1", "TMF", "BUY", 5, 48000, "TMF202610")]
    pf = build_portfolio(rows)
    assert pf.cash < 9_500_000 and pf.futures_cash < 500_000
    prices = {"2330": 2500, "TMF202610": 48000}
    nav = pf.nav(prices)
    assert pf.holding_ratio(prices) == pytest.approx(3000 * 2500 / nav)
    assert pf.trade_count == 2  # 資金調撥不算交易


def test_position_alerts_margin_call_and_expiry(monkeypatch):
    import datetime as dt

    from src.agents import alerts
    from src.agents import futures_data as fd

    monkeypatch.setattr(fd, "futures_quotes", lambda root: {"spot": 47000.0, "months": {}})
    rows = [{"date": "d0", "code": "TRANSFER", "side": "TO_SECURITIES", "shares": "1", "price": "1900000", "margin": "0"},
            t("d1", "MTX", "BUY", 1, 48000, "MTX202610"), t("d1", "TXO", "BUY", 1, 100, "TXO202610P47200")]
    pf = build_portfolio(rows)
    m = {"MTX": {"initial": 175250, "maintenance": 134500}}
    a = alerts.position_alerts(pf, {}, {"MTX202610": 47000, "TXO202610P47200": 20}, m, {}, dt.date(2026, 10, 20))
    titles = " ".join(x["title"] for x in a)
    assert any(x["level"] == "嚴重" and "追繳" in x["title"] for x in a)  # 權益數低於維持保證金
    assert "剩 1 天" in titles and "權利金剩" in titles and "履約價" in titles
