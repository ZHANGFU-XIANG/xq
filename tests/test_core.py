"""核心計算單元測試：成本、投資組合重播、張數、規則檢查、數字核對。"""
import datetime as dt

import pytest

from src.agents import compliance, sizer
from src.agents.verifier import verify
from src.state.portfolio import build_portfolio, trade_cost


def test_trade_cost_stock_and_etf():
    # 股票賣出：手續費 0.1425% + 證交稅 0.3%
    assert trade_cost("2330", "SELL", 1000, 1000) == round(1_000_000 * 0.001425) + 3000
    # ETF 賣出稅 0.1%
    assert trade_cost("0050", "SELL", 1000, 100) == round(100_000 * 0.001425) + 100
    # 最低手續費 20 元
    assert trade_cost("2303", "BUY", 1, 50) == 20


def test_portfolio_replay_cash_and_margin():
    trades = [
        {"date": "2026-10-05", "code": "2303", "side": "BUY", "shares": "2000", "price": "50", "margin": "0"},
        {"date": "2026-10-05", "code": "2382", "side": "BUY", "shares": "1000", "price": "300", "margin": "1"},
        {"date": "2026-10-06", "code": "2303", "side": "SELL", "shares": "1000", "price": "55", "margin": "0"},
    ]
    pf = build_portfolio(trades)
    assert pf.trade_count == 3 and pf.traded_symbols == {"2303", "2382"}
    assert pf.positions["2382"].margin_loan == 180_000  # 融資 60%
    buy1 = 100_000 + trade_cost("2303", "BUY", 2000, 50)
    buy2 = 300_000 + trade_cost("2382", "BUY", 1000, 300) - 180_000
    sell = 55_000 - trade_cost("2303", "SELL", 1000, 55)
    assert pf.cash == pytest.approx(8_000_000 - buy1 - buy2 + sell)
    prices = {"2303": 55, "2382": 300}
    nav = pf.nav(prices)
    assert nav == pytest.approx(pf.cash + 2_000_000 + 55_000 + 300_000 - 180_000)


def test_oversell_rejected():
    with pytest.raises(ValueError):
        build_portfolio([{"date": "d", "code": "2303", "side": "SELL", "shares": "10", "price": "1", "margin": "0"}])


def test_tick_rounding():
    assert sizer.round_tick(1337.24 * 1.005, up=True) == 1345.0
    assert sizer.round_tick(162.33) == 162.0
    assert sizer.round_tick(48.12, up=True) == 48.15
    assert sizer.round_tick(48.12, etf=True) == 48.12


def test_split_lots_and_format():
    assert sizer.split_lots(13257, True) == (13, 257)
    assert sizer.split_lots(13257, False) == (13, 0)
    assert sizer.fmt_shares(2000) == "2 張（2,000 股）"
    assert sizer.fmt_shares(500) == "500 股（零股）"


def test_round_sell():
    assert sizer._round_sell(4000 / 3, 4000, False) == 1000
    assert sizer._round_sell(400, 4000, False) == 1000  # 至少一張
    assert sizer._round_sell(5000, 4000, False) == 4000
    assert sizer._round_sell(487.6, 1463, True) == 487


def test_warrant_detection():
    assert compliance.is_warrant("030123")
    assert not compliance.is_warrant("006208")  # ETF
    assert not compliance.is_warrant("2330")
    assert not compliance.is_warrant("00981A")


def test_compliance_flags_low_holding_after_build_period():
    pf = build_portfolio([])
    res = compliance.check(pf, {}, [], dt.date(2026, 10, 20), 10_000_000)
    item = next(c for c in res if c["item"].startswith("目前持股率"))
    assert item["status"] == "未通過"
    res = compliance.check(pf, {}, [], dt.date(2026, 10, 6), 10_000_000)
    item = next(c for c in res if c["item"].startswith("目前持股率"))
    assert item["status"] == "注意"  # 建倉期不罰


def test_remaining_trading_days_matches_rules():
    assert compliance.remaining_trading_days(dt.date(2026, 10, 5)) == 38


def test_verifier_blocks_invented_numbers():
    facts = {"orders": [{"code": "3037", "shares": 1463, "limit_price": 1345.0}], "x": 0.0069, "t": "命中率 80.2%"}
    assert verify("買進 3037 共 1,463 股，限價 1345，大盤 +0.69%，命中率 80.2%", facts)[0]
    ok, bad = verify("目標價 1600 元，預估 EPS 45.3", facts)
    assert not ok and "1600" in bad and "45.3" in bad
    ok, bad = verify("法人連續買超 7 日", facts)  # 小整數也必須有出處
    assert not ok and "7" in bad
