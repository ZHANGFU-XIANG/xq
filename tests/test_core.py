"""核心計算單元測試：成本、投資組合重播、張數、規則檢查、數字核對。"""
import datetime as dt

import pytest

from src.agents import compliance, sizer
from src.agents.verifier import verify
from src.state.portfolio import build_portfolio, trade_cost


def test_trade_cost_stock_and_etf():
    # 股票賣出：手續費 0.1425%（無條件進位，與競賽 App 相同）+ 證交稅 0.3%
    assert trade_cost("2330", "SELL", 1000, 1000) == 1425 + 3000
    # ETF 賣出稅 0.1%；142.5 → 143（進位）
    assert trade_cost("0050", "SELL", 1000, 100) == 143 + 100
    # 成交價金捨去：528 股 × 28.6 = 15,100.8 → 15,100（App 成本），手續費 21.5 → 22
    from src.state.portfolio import trade_amount
    assert trade_amount(528, 28.6) == 15100 and trade_cost("3591", "BUY", 528, 28.6) == 22
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


def test_news_finance_filter():
    from src.agents.news import is_finance

    g = "Google新聞:中國 經濟"
    assert is_finance({"source": g, "title": "9月就業僅增2.9萬人 Fed可能放緩升息 - news.cnyes.com"})
    assert is_finance({"source": "鉅亨網台股", "title": "台積電法說會前瞻"})
    assert not is_finance({"source": g, "title": "北京在寮國設立首個境外空軍設施 - 經濟日報"})  # 媒體名不算內容
    assert not is_finance({"source": g, "title": "黃仁勳愛女驚傳新婚 | 財經 | CTWANT - CTWANT"})
    assert not is_finance({"source": "中央社國際", "title": "奈及利亞中部發生重大車禍 造成20人喪命"})


def test_stock_info_falls_back_and_never_caches_empty(monkeypatch, tmp_path):
    from src.agents import universe

    class Bad:
        def json(self):
            return {"msg": "Requests reach the upper limit"}

    monkeypatch.setattr(universe, "_info", None)
    monkeypatch.setattr(universe, "INFO_PKL", tmp_path / "info.pkl")
    from src.data import providers as pv

    monkeypatch.setattr(pv, "finmind_get", lambda *a, **k: None)
    monkeypatch.setattr(universe, "_isin_info", lambda: {})
    assert universe.stock_info() == {} and not (tmp_path / "info.pkl").exists()
    big = {str(1000 + i): {"code": str(1000 + i), "name": "x", "industry": "半導體業", "raw_industry": "半導體業",
                           "market": "TWSE", "is_etf": False} for i in range(600)}
    monkeypatch.setattr(universe, "_isin_info", lambda: big)
    assert len(universe.stock_info()) == 600 and (tmp_path / "info.pkl").exists()


def test_finmind_rotation_and_fallback(monkeypatch):
    """第一組 token 額度用完 → 冷卻並改用下一組；全部冷卻 → 回傳 None，由官方來源補上。"""
    from src.data import providers as pv

    calls = []

    class Resp:
        def __init__(self, tok):
            self.tok = tok

        def json(self):
            if self.tok == "Bearer A":
                return {"status": 402, "msg": "Requests reach the upper limit."}
            return {"status": 200, "data": [{"x": 1}]}

    def fake_get(url, params=None, timeout=None, headers=None):
        tok = (headers or {}).get("Authorization")
        calls.append(tok)
        return Resp(tok)

    monkeypatch.setenv("FINMIND_TOKENS", "A,B")
    monkeypatch.setattr(pv.requests, "get", fake_get)
    monkeypatch.setattr(pv, "_COOL", {})
    monkeypatch.setitem(pv._NEXT, "i", 0)
    df = pv.finmind_get("X", "2330", "2026-01-01")
    assert df is not None and len(df) == 1 and calls == ["Bearer A", "Bearer B"]
    assert pv._COOL.get("token1", 0) > 0 and pv.finmind_available()
    calls.clear()
    pv.finmind_get("X", "2330", "2026-01-01")
    assert "Bearer A" not in calls  # 冷卻中的 token 不再使用
    for lab in ("token1", "token2", "匿名"):
        pv._COOL[lab] = 9e12
    assert pv.finmind_get("X", "2330") is None and not pv.finmind_available()


def test_market_data_falls_back_to_official(monkeypatch, tmp_path):
    from src.agents import market_data as md
    from src.data import providers as pv

    monkeypatch.setattr(md, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(pv, "finmind_get", lambda *a, **k: None)
    monkeypatch.setattr(pv, "revenue_all", lambda: {"9999": {"month": "2026/08", "revenue_100m": 1.0, "yoy": 0.1, "mom": 0.0, "yoy_3m": None}})
    monkeypatch.setattr(pv, "valuation_all", lambda: {"9999": {"date": "2026-10-02", "per": 10.0, "pbr": 1.0, "dividend_yield": 3.0}})
    assert md.tw_revenue("9999")["yoy"] == 0.1 and md.tw_valuation("9999")["per"] == 10.0
    assert pv.source_of("revenue", "9999") == md.OFFICIAL


def test_cached_skips_empty(tmp_path, monkeypatch):
    import pandas as pd

    from src.agents import market_data as md

    monkeypatch.setattr(md, "CACHE_DIR", tmp_path)
    md._cached("k_empty", lambda: pd.DataFrame())
    md._cached("k_full", lambda: {"a": 1})
    assert not (tmp_path / "k_empty.pkl").exists() and (tmp_path / "k_full.pkl").exists()


def test_news_asset_category():
    from src.agents.news import asset_category

    assert asset_category({"title": "外資台指期未平倉空單增加 - 鉅亨"}) == "期貨"
    assert asset_category({"title": "00878 季配息公布 殖利率 7%"}) == "ETF"
    assert asset_category({"title": "台積電法說會前瞻"}) == "股票"


def test_schedule_gate_windows(monkeypatch, tmp_path):
    import datetime as dt

    from src import schedule
    from src.config import TW_TZ

    monkeypatch.setattr(schedule, "LOG", tmp_path / "log.json")
    at = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=TW_TZ)  # noqa: E731
    assert schedule.gate("premarket", at("2026-10-05 07:01"))[0]
    assert not schedule.gate("preopen", at("2026-10-05 13:52"))[0]       # 延遲到收盤後的開盤前更新沒有意義
    run, key, _ = schedule.gate("intraday", at("2026-10-05 09:45"))
    assert run and key == "intraday 09:40"
    assert not schedule.gate("intraday", at("2026-10-05 14:25"))[0]      # 收盤後才觸發的盤中建議略過
    schedule.mark(key, at("2026-10-05 09:47"))
    assert not schedule.gate("intraday", at("2026-10-05 09:50"))[0]      # 同一時段不重複
    assert schedule.gate("intraday", at("2026-10-05 10:21"))[0]
    assert not schedule.gate("intraday", at("2026-10-10 09:00"))[0]      # 國慶日休市


def test_lots_keep_separate_cost_and_sell_fifo(monkeypatch, tmp_path):
    from src.state import portfolio as pfm

    trades = [{"date": "2026-10-05", "code": "6213", "side": "BUY", "shares": "571", "price": "723", "margin": "0"},
              {"date": "2026-10-05", "code": "6213", "side": "BUY", "shares": "1000", "price": "726", "margin": "0"},
              {"date": "2026-10-06", "code": "6213", "side": "SELL", "shares": "600", "price": "750", "margin": "0"}]
    pos = pfm.build_portfolio(trades[:2]).positions["6213"]
    assert [(lot.shares, lot.price) for lot in pos.lots] == [(571, 723.0), (1000, 726.0)]
    assert abs(pos.cost_basis - sum(lot.cost_basis for lot in pos.lots)) < 1e-6
    pos = pfm.build_portfolio(trades).positions["6213"]  # 先進先出：先賣完 571 股那批，再從 1000 股那批扣 29
    assert [(lot.shares, lot.price) for lot in pos.lots] == [(971, 726.0)]
    assert abs(pos.cost_basis - pos.lots[0].cost_basis) < 1e-6
