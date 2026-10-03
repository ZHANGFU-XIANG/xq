"""讀取每日報告（facts JSON）與投資組合狀態，整理成前端／AI 助理需要的格式。"""
from __future__ import annotations

import json
from functools import lru_cache

from src.agents import on_demand
from src.config import FACTS_DIR, REPORT_DIR, rules, stock_meta
from src.state import params
from src.state import portfolio as pfm


def report_files() -> list[dict]:
    out = []
    for fp in sorted(FACTS_DIR.glob("*.json"), reverse=True):
        date, title = fp.stem.split("_", 1)
        out.append({"date": date, "title": title, "key": fp.stem})
    return out


@lru_cache(maxsize=32)
def _load(key: str, mtime: float) -> dict:
    return json.loads((FACTS_DIR / f"{key}.json").read_text(encoding="utf-8"))


def load_report(key: str | None = None) -> dict | None:
    files = report_files()
    if not files:
        return None
    if key is None:
        pre = [f for f in files if f["title"] == "盤前報告"]
        key = (pre or files)[0]["key"]
    elif len(key) == 10:  # 只給日期 → 該日盤前報告優先
        same = [f for f in files if f["date"] == key]
        if not same:
            return None
        key = next((f["key"] for f in same if f["title"] == "盤前報告"), same[0]["key"])
    fp = FACTS_DIR / f"{key}.json"
    if not fp.exists():
        return None
    data = _load(key, fp.stat().st_mtime)
    stem = fp.stem
    month = data["date"][:7]
    data = dict(data)
    data["files"] = {ext: f"{month}/{stem}{suffix}" for ext, suffix in
                     (("docx", ".docx"), ("pptx", ".pptx"), ("csv", "_下單清單.csv"))
                     if (REPORT_DIR / month / f"{stem}{suffix}").exists()}
    data["key"] = key
    return data


def report_summary(date: str | None = None) -> dict:
    r = load_report(date)
    if not r:
        return {"error": "查無報告"}
    return {
        "date": r["date"], "title": r["title"], "allocation": {k: r["allocation"][k] for k in ("offense", "defense", "reasons")},
        "taiex_prediction": r["predictions"].get("TAIEX"),
        "orders": [{k: o.get(k) for k in ("action", "basket_name", "code", "name", "shares", "lots", "odd", "limit_price",
                                          "stop", "amount", "weight_pct", "why_sell")} for o in r["orders"]],
        "compliance": r["compliance"], "top_picks": r["top_picks"][:10], "def_picks": r.get("def_picks", [])[:8],
        "summary": r.get("text", {}).get("market_summary"), "outlook": r.get("text", {}).get("outlook"),
    }


def portfolio() -> dict:
    pf = pfm.build_portfolio()
    meta = pfm.load_meta()
    prices = {}
    for c in pf.positions:
        df = on_demand.price_df(c)
        if not df.empty:
            prices[c] = float(df.Close.iloc[-1])
    nav = pf.nav(prices)
    pos = []
    for c, p in pf.positions.items():
        price = prices.get(c, p.avg_cost)
        pos.append({"code": c, "name": stock_meta(c)["name"], "shares": p.shares, "avg_cost": round(p.avg_cost, 2),
                    "price": price, "market_value": round(p.shares * price), "pnl": round(p.shares * price - p.cost_basis),
                    "pnl_pct": round((price / p.avg_cost - 1) * 100, 2) if p.avg_cost else 0,
                    "weight_pct": round(p.shares * price / nav * 100, 2) if nav else 0,
                    "basket": params.basket(meta.get(c, {}).get("basket") or "offense")["name"],
                    "stop": meta.get(c, {}).get("stop"), "margin_loan": round(p.margin_loan)})
    initial = rules()["capital"]["initial"]
    return {"nav": round(nav), "return_pct": round((nav / initial - 1) * 100, 3), "cash": round(pf.cash),
            "futures_cash": round(pf.futures_cash), "holding_ratio": round(pf.holding_ratio(prices), 4),
            "margin_loan": round(pf.margin_loan()), "realized_pnl": round(pf.realized_pnl), "trade_count": pf.trade_count,
            "traded_symbols": sorted(pf.traded_symbols), "positions": pos, "trades": pf.trades[-50:][::-1],
            "nav_history": pfm.load_history()}


def macro_latest() -> dict:
    r = load_report()
    if not r:
        return {"error": "查無報告"}
    return {"date": r["date"], "events": r.get("macro", []), "analysis": r.get("text", {}).get("macro_analysis"),
            "hypothetical": r.get("hypothetical")}


_TRACK: dict = {"t": 0.0, "v": None}


def track_record() -> dict:
    """實際表現（含回補歷史）：依預測紀錄即時對答案，30 分鐘快取。"""
    import time

    import pandas as pd

    from src.agents import tuner

    if _TRACK["v"] is not None and time.time() - _TRACK["t"] < 1800:
        return _TRACK["v"]
    ctx = on_demand.context()
    ev = tuner.evaluate(ctx["panel"], ctx["taiex"])
    monthly = []
    if ev["alpha"]:
        df = pd.DataFrame(ev["alpha"])
        df["month"] = pd.to_datetime(df["target_date"]).dt.strftime("%Y-%m")
        for (b, m), g in df.groupby(["basket", "month"]):
            monthly.append({"basket": b, "month": m, "n": int(len(g)), "avg_ret": round(float(g.real_ret.mean()), 4),
                            "avg_excess": round(float(g.real_excess.mean()), 4), "up_rate": round(float((g.real_ret > 0).mean()), 3),
                            "newhigh_rate": round(float(g.real_newhigh.mean()), 3)})
    out = {"stats": tuner.summary_stats(ev), "monthly": monthly,
           "backfill_note": "包含回補的歷史預測（模型只用當天以前的資料訓練，不偷看未來）"}
    _TRACK.update(t=time.time(), v=out)
    return out


def model_health() -> dict:
    from src.agents import tuner

    r = load_report() or {}
    am = r.get("alpha_model", {})
    tr = track_record()
    return {"date": r.get("date"), "backtest": am.get("metrics"), "base_rate": am.get("base_rate"),
            "n_stocks": am.get("n_stocks"), "feature_ic": am.get("feature_ic"),
            "live": tr["stats"], "monthly": tr["monthly"], "backfill_note": tr["backfill_note"],
            "tuning_log": tuner.history()[-10:], "learned_params": params.learned()}


def news(date: str | None = None) -> dict:
    r = load_report(date)
    if not r:
        return {"news": [], "events": [], "macro": []}
    from src.agents.news import is_finance

    def keep(events):  # 事件只保留財經新聞佐證；全部被濾掉的事件不顯示
        out = []
        for e in events or []:
            ns = [n for n in e.get("news", []) if is_finance(n)]
            if ns or not e.get("news"):
                out.append({**e, "news": ns})
        return out

    return {"date": r["date"], "news": [n for n in r.get("news", []) if is_finance(n)], "events": keep(r.get("events")),
            "macro": keep(r.get("macro")),
            "market_view": r.get("market_view"), "us_snapshot": r.get("us_snapshot", []),
            "market_summary": (r.get("text") or {}).get("market_summary")}


_ALERTS: dict = {"t": 0.0, "v": None}


def live_alerts(refresh: bool = False) -> dict:
    """即時警示（5 分鐘快取）：持股與今日建議以盤中即時價判斷，全市場漲跌停用最近收盤。"""
    import time

    from src.agents import alerts

    if not refresh and _ALERTS["v"] is not None and time.time() - _ALERTS["t"] < 300:
        return _ALERTS["v"]
    r = load_report() or {}
    out = alerts.collect(pfm.build_portfolio(), pfm.load_meta(), r.get("orders", []), r.get("us_snapshot", []),
                         on_demand.current_target())
    _ALERTS.update(t=time.time(), v=out)
    return out


_INTRA: dict = {"slot": None, "key": None, "v": None}


def live_intraday(refresh: bool = False) -> dict:
    """盤中建議：每 40 分鐘一個時段，時段內建議固定（行情另外即時刷新）；持股變動或手動重算時立即更新。"""
    from src.agents import intraday

    slot = intraday.current_slot()
    pf = pfm.build_portfolio()
    key = tuple(sorted((c, p.shares) for c, p in pf.positions.items()))
    if not refresh and _INTRA["v"] is not None and _INTRA["slot"] == slot and _INTRA["key"] == key:
        return _INTRA["v"]
    out = intraday.run(load_report())
    _INTRA.update(slot=slot, key=key, v=out)
    return out


_QUOTES: dict = {}
INDEX_CHANNELS = {"TAIEX": ("tse_t00.tw", "加權指數"), "OTC": ("otc_o00.tw", "櫃買指數")}


def live_quotes(codes: list[str]) -> dict:
    """即時報價（證交所 MIS，8 秒快取）＋加權、櫃買指數。"""
    import time

    import requests

    from src.agents import alerts

    codes = sorted({c for c in codes if c})[:60]
    key = ",".join(codes)
    hit = _QUOTES.get(key)
    if hit and time.time() - hit[0] < 8:
        return hit[1]
    quotes = alerts.realtime(codes) if codes else {}
    idx = {}
    try:
        r = requests.get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                         params={"ex_ch": "|".join(c for c, _ in INDEX_CHANNELS.values()), "json": 1, "delay": 0}, timeout=10).json()
        names = {ch.split(".")[0].split("_")[1]: (k, n) for k, (ch, n) in INDEX_CHANNELS.items()}
        for m in r.get("msgArray", []):
            k, n = names.get(m.get("c"), (None, None))
            last = float(m.get("z") or m.get("y") or 0)
            prev = float(m.get("y") or 0)
            if k and last and prev:
                idx[k] = {"name": n, "price": round(last, 2), "change": round(last - prev, 2),
                          "chg_pct": round((last / prev - 1) * 100, 2), "time": m.get("t")}
    except Exception:  # noqa: BLE001
        pass
    out = {"quotes": quotes, "indices": idx, "trading": alerts.is_trading_hours()}
    _QUOTES[key] = (time.time(), out)
    return out


_NEWS: dict = {"t": 0.0, "v": None}


def live_news(refresh: bool = False) -> dict:
    """即時新聞：5 分鐘自動更新；refresh=True（使用者手動重新整理）時立即抓取，但 30 秒內不重複抓。"""
    import time

    from src.agents import news as news_agent
    from src.config import now_tw

    age = time.time() - _NEWS["t"]
    if _NEWS["v"] is None or age > 300 or (refresh and age > 30):
        items = news_agent.fetch_news(now_tw())
        _NEWS.update(t=time.time(), v={"news": [{k: n[k] for k in ("id", "source", "title", "url", "time")} for n in items],
                                       "fetched_at": now_tw().strftime("%H:%M:%S")})
    return _NEWS["v"]


_INTL: dict = {}


def intl_impacts(codes: list[str]) -> dict:
    """個股受國際市場影響的註記：歷史敏感度 β × 最近一個交易日的國際因子變動，只列統計顯著且影響 ≥0.4% 者。"""
    import time

    import pandas as pd

    from src.agents import macro
    from src.config import settings

    ctx = on_demand.context()
    target = pd.Timestamp(ctx["target"])
    codes = sorted({c for c in codes if c})[:40]
    key = (ctx["target"], ",".join(codes))
    hit = _INTL.get(key)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    sens = macro.sensitivities(codes, ctx["panel"], ctx["us_hist"], target)
    moves = {}
    for k, cfg in settings()["macro_factors"].items():
        df = ctx["us_hist"].get(cfg["ticker"])
        if df is None:
            continue
        d = df[df.index < target] if cfg["ticker"] not in macro.ASIA else df[df.index <= target]
        if len(d) < 2:
            continue
        last, prev = float(d.Close.iloc[-1]), float(d.Close.iloc[-2])
        moves[k] = {"move": (last - prev) if cfg["mode"] == "diff" else (last / prev - 1), "name": cfg["name"],
                    "date": str(d.index[-1].date()),
                    "label": f"{last - prev:+.2f}" if cfg["mode"] == "diff" else f"{(last / prev - 1) * 100:+.2f}%"}
    out = {}
    for c in codes:
        notes = []
        for k, s in sens.get(c, {}).items():
            if abs(s["corr"]) < macro.MIN_CORR or k not in moves:
                continue
            impact = s["beta"] * moves[k]["move"] * 100
            if abs(impact) >= 0.4:
                region = "日本" if k == "JAPAN" else "中國" if k == "CHINA" else "國際" if k in ("OIL", "GOLD", "USD_TWD") else "美國"
                notes.append({"factor": moves[k]["name"], "region": region, "move": moves[k]["label"], "impact_pct": round(impact, 2),
                              "text": f"{moves[k]['name']} {moves[k]['label']}，依歷史連動估 {impact:+.1f}%"})
        notes.sort(key=lambda n: -abs(n["impact_pct"]))
        if notes:
            out[c] = notes[:2]
    res = {"impacts": out, "factor_date": max((m["date"] for m in moves.values()), default=None)}
    _INTL[key] = (time.time(), res)
    return res
