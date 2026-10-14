"""共用流程：每日報告（src/main.py）與即時個股分析／AI 助理（src/agents/on_demand.py）都經過這裡，
確保兩者用同一套資料、模型與評分邏輯。"""
from __future__ import annotations

import datetime as dt
import logging
import threading

import pandas as pd

from src.agents import allocation, alpha_model, compliance, etf, market_data, predictor, strategy, tuner, universe
from src.config import rules, stock_meta
from src.state import portfolio as pfm

log = logging.getLogger(__name__)
_CTX: dict[dt.date, dict] = {}
_LOCK = threading.Lock()


def market_context(target: dt.date, use_memory: bool = False) -> dict:
    """全市場資料與一個月模型。use_memory=True 時同一目標日只算一次（給常駐的 API 伺服器用）。"""
    if use_memory:
        with _LOCK:  # 伺服器多個請求同時進來時只算一次
            if target not in _CTX:
                ctx = market_context(target, use_memory=False)
                _CTX.clear()
                _CTX[target] = ctx
            return _CTX[target]
    us_hist = market_data.us_history()
    panel = universe.load_panel()
    market_data.set_panel(panel)
    tx = market_data.taiex(3)
    try:  # DataGuard：資料過期或和交易所不一致就先補正（10/08～10/14 盤後用了前一天價格才加上）
        from src.agents import data_guard

        pf0 = pfm.build_portfolio()
        guard = data_guard.check(panel, sorted(panel), list(pf0.positions), tx, list(pf0.derivs))
    except Exception as e:  # noqa: BLE001
        log.warning("DataGuard 失敗：%s", e)
        guard = {"level": "unknown", "issues": [{"level": "警告", "msg": f"資料檢查失敗：{str(e)[:100]}"}]}
    codes = universe.liquid_universe(panel, target)
    etf = {c for c in codes if stock_meta(c)["is_etf"]}
    # 手上的持股就算變成處置股（被排除在選股池外）也要有預測，結單轉股才能比較（例：10/12 聯茂）
    try:
        held = [c for c in pfm.build_portfolio().positions if c not in codes and len(panel.get(c, [])) >= 300]
    except Exception:  # noqa: BLE001
        held = []
    alpha_out = alpha_model.run(panel, codes + held, tx.Close, target, etf)
    return {"us_hist": us_hist, "panel": panel, "codes": codes, "etf": etf, "taiex": tx, "alpha": alpha_out,
            "target": target, "data_guard": guard}


def score_codes(codes: list[str], ctx: dict, target: dt.date, senti_by_code: dict | None = None) -> dict:
    """特徵 → 月營收 → 隔夜預測 → 進攻/穩健雙籃評分 → 排序。"""
    senti_by_code = senti_by_code or {}
    alpha_out = ctx["alpha"]
    a_preds = alpha_out["predictions"]
    us_snap = market_data.us_snapshot(ctx["us_hist"], target)
    us_by = {r["ticker"]: r for r in us_snap}
    feats = {c: market_data.tw_features(c, target) for c in codes}
    feats = {c: f for c, f in feats.items() if f}
    revenue = {c: market_data.tw_revenue(c) for c in feats if not stock_meta(c)["is_etf"]}
    preds = predictor.predict(list(feats), ctx["us_hist"], target)
    scored = [strategy.score_stock(c, feats[c], a_preds.get(c), preds.get(c), senti_by_code.get(c), revenue.get(c),
                                   us_by, alpha_out["base_rate"]) for c in feats]
    for s in scored:
        s.update(strategy.score_defense(s, alpha_out["up_base_rate"]))
    ranked = strategy.rank(scored)
    etf.annotate(ranked)
    return {"feats": feats, "prices": {c: f["close"] for c, f in feats.items()}, "revenue": revenue, "preds": preds,
            "ranked": ranked, "scores": {s["code"]: s for s in ranked}, "us_snap": us_snap, "us_by": us_by}


def candidate_codes(ctx: dict, pf: pfm.Portfolio) -> list[str]:
    from src.state import params

    a_preds = ctx["alpha"]["predictions"]
    top = sorted(a_preds, key=lambda c: -a_preds[c]["combo"])[: params.alpha()["candidates"]]
    top_def = sorted([c for c in a_preds if a_preds[c]["eligible_def"]],
                     key=lambda c: -a_preds[c]["def_combo"])[: params.basket("defense")["candidates"]]
    etfs = sorted([c for c in a_preds if c in ctx["etf"]],
                  key=lambda c: -a_preds[c].get("def_combo", a_preds[c]["combo"]))[: params.basket("etf")["candidates"]]
    return list(dict.fromkeys(list(pf.positions) + top + top_def + etfs + params.strategy()["etf_fillers"]))


def basket_of(pf: pfm.Portfolio, meta: dict, scores: dict) -> dict[str, str]:
    """持股所屬籃子：進場時記錄；舊部位依兩籃分數較高者歸類（會回寫 meta）。"""
    out = {}
    for c in pf.positions:
        b = meta.get(c, {}).get("basket")
        if stock_meta(c).get("is_etf"):
            b = "etf"  # ETF 一律歸 ETF 籃（舊紀錄可能記在穩健籃）
        if not b:
            s = scores.get(c, {})
            b = "defense" if s.get("def_score", -9) > s.get("score", -9) else "offense"
            if c in meta:
                meta[c]["basket"] = b
        out[c] = b
    return out


def with_marks(pf: pfm.Portfolio, prices: dict) -> dict:
    """股票價格＋持有期貨／選擇權的最新報價（給 nav 計算）。"""
    if not pf.derivs:
        return prices
    from src.agents import futures_data

    return {**prices, **futures_data.marks(pf)}


def derivatives_plan(pf: pfm.Portfolio, prices: dict, nav: float, alloc: dict, scores: dict, us_snap: list[dict],
                     today: dt.date) -> dict:
    """期貨方向與賣權避險建議。避險規模用「股票＋ETF 目標持股」的 β 加權市值估算（持股用實際 β，其餘以 β=1 計）。"""
    from src.agents import futures
    from src.state import params

    def beta(c: str) -> float:
        b = (((scores.get(c) or {}).get("alpha") or {}).get("features") or {}).get("beta60")
        return float(b) if b is not None else 1.0

    held = sum(p.shares * prices.get(c, 0) * beta(c) for c, p in pf.positions.items())
    held_mv = pf.market_value(prices)
    target = params.strategy()["target_holding_ratio"] * nav
    beta_value = held + max(0.0, target - held_mv)
    try:
        return futures.plan(pf, prices, nav, alloc, beta_value, today, us_snap)
    except Exception as e:  # noqa: BLE001  期貨失敗不影響股票建議
        import logging

        logging.getLogger(__name__).warning("期貨建議失敗：%s", e)
        return {"orders": [], "hedge": None, "signal": None, "reasons": [f"期貨建議暫時無法計算：{str(e)[:80]}"],
                "summary": "期貨建議暫時無法計算", "reserve": params.get("futures")["min_reserve"], "margin_needed": 0}


def current_allocation(ctx: dict, target: dt.date, today: dt.date, nav: float) -> dict:
    recent = [e for e in tuner.history() if e.get("issues")]
    last_issues = recent[-1]["issues"] if recent and recent[-1]["date"] >= (today - dt.timedelta(days=5)).isoformat() else []
    return allocation.decide(ctx["taiex"], pd.Timestamp(target), pfm.load_history(), rules()["capital"]["initial"], nav,
                             compliance.remaining_trading_days(today), last_issues, us_hist=ctx.get("us_hist"))
