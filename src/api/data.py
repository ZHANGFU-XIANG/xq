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
    marks = {}
    derivs = []
    if pf.derivs:
        from src.agents import futures_data

        marks = futures_data.marks(pf)
        margins = futures_data.margins()
        for c, d in pf.derivs.items():
            px = marks.get(c)
            derivs.append({"contract": c, "root": d.root, "kind": "期貨" if d.kind == "FUT" else "賣權（買方）",
                           "qty": d.qty, "avg_price": round(d.avg_price, 2), "price": px,
                           "value": round(d.value(px)), "multiplier": d.multiplier,
                           "margin": round(abs(d.qty) * margins.get(d.root, {}).get("initial", 0)) if d.kind == "FUT" else 0})
    nav = pf.nav({**prices, **marks})
    pos = []
    for c, p in pf.positions.items():
        price = prices.get(c, p.avg_cost)
        pos.append({"code": c, "name": stock_meta(c)["name"], "shares": p.shares, "avg_cost": round(p.avg_cost, 2),
                    "price": price, "market_value": round(p.shares * price), "pnl": round(p.shares * price - p.cost_basis),
                    "pnl_pct": round((price / p.avg_cost - 1) * 100, 2) if p.avg_cost else 0,
                    "weight_pct": round(p.shares * price / nav * 100, 2) if nav else 0,
                    "asset": "ETF" if stock_meta(c).get("is_etf") else "STOCK",
                    "basket": params.basket("etf" if stock_meta(c).get("is_etf") else (meta.get(c, {}).get("basket") or "offense"))["name"],
                    "stop": meta.get(c, {}).get("stop"), "margin_loan": round(p.margin_loan)})
    initial = rules()["capital"]["initial"]
    return {"nav": round(nav), "return_pct": round((nav / initial - 1) * 100, 3), "cash": round(pf.cash),
            "futures_cash": round(pf.futures_cash), "futures_equity": round(pf.futures_equity(marks)), "derivatives": derivs,
            "holding_ratio": round(pf.holding_ratio({**prices, **marks}), 4),
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

    from src.agents.news import asset_category

    return {"date": r["date"], "news": [{**n, "category": n.get("category") or asset_category(n)} for n in r.get("news", [])
                                        if is_finance(n)], "events": keep(r.get("events")),
            "macro": keep(r.get("macro")),
            "market_view": r.get("market_view"), "us_snapshot": r.get("us_snapshot", []),
            "market_summary": (r.get("text") or {}).get("market_summary")}


_ALERTS: dict = {"t": 0.0, "v": None}


_ALERTS_LOCK = __import__("threading").Lock()


def live_alerts(refresh: bool = False) -> dict:
    """即時警示（5 分鐘快取）：持股與今日建議以盤中即時價判斷，全市場漲跌停用最近收盤。"""
    import time

    from src.agents import alerts

    if not refresh and _ALERTS["v"] is not None and time.time() - _ALERTS["t"] < 300:
        return _ALERTS["v"]
    r = load_report() or {}

    def compute():
        with _ALERTS_LOCK:
            out = alerts.collect(pfm.build_portfolio(), pfm.load_meta(), r.get("orders", []), r.get("us_snapshot", []),
                                 on_demand.current_target())
            _ALERTS.update(t=time.time(), v=out)
            return out

    prev = _ALERTS["v"] or ((r.get("alerts") or None) and {**r["alerts"], "realtime": {}})
    if refresh or prev is None:
        return compute()
    if not _ALERTS_LOCK.locked():  # 先回上一份（或報告內的警示），背景更新
        __import__("threading").Thread(target=compute, daemon=True).start()
    return {**prev, "stale": True}


_INTRA: dict = {"slot": None, "key": None, "v": None}
_INTRA_LOCK = __import__("threading").Lock()


def _saved_intraday() -> dict | None:
    """雲端排程每 40 分鐘存下的最新一份盤中建議（data/intraday/*.json，經 GitHub 同步到網站）。"""
    from src.config import DATA_DIR

    files = sorted((DATA_DIR / "intraday").glob("*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _compute_intraday(slot: str, key: tuple) -> dict:
    from src.agents import intraday

    with _INTRA_LOCK:  # 同一時間只算一份（全市場重算約 1～1.5 分鐘）
        if _INTRA["v"] is not None and _INTRA["slot"] == slot and _INTRA["key"] == key and not _INTRA["v"].get("stale"):
            return _INTRA["v"]
        out = intraday.run(load_report())
        _INTRA.update(slot=slot, key=key, v=out)
        return out


def live_intraday(refresh: bool = False) -> dict:
    """盤中建議：每 40 分鐘一個時段，時段內建議固定（行情另外即時刷新）；持股變動或手動重算時立即更新。
    還沒有這個時段的結果時，先回傳上一份（記憶體或雲端存檔）並標示 stale，背景重算完自動替換，不讓使用者等。"""
    import threading

    from src.agents import intraday

    slot = intraday.current_slot()
    pf = pfm.build_portfolio()
    key = tuple(sorted((c, p.shares) for c, p in pf.positions.items()))
    fresh = _INTRA["v"] is not None and _INTRA["slot"] == slot and _INTRA["key"] == key
    if fresh and not refresh:
        return _INTRA["v"]
    if refresh:
        _INTRA["v"] = None
        return _compute_intraday(slot, key)
    prev = _INTRA["v"] or _saved_intraday()
    if prev is None:
        return _compute_intraday(slot, key)
    if not _INTRA_LOCK.locked():
        threading.Thread(target=_compute_intraday, args=(slot, key), daemon=True).start()
    return {**prev, "stale": True, "cached_from": prev.get("time")}


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
        _NEWS.update(t=time.time(), v={"news": [{k: n[k] for k in ("id", "source", "title", "url", "time", "category")}
                                                for n in items],
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


def potential(limit: int = 8) -> dict:
    """潛在標的：模型看好但今天沒有列入下單的股票（候補遞補、過熱等回檔），附「為什麼還沒買」與觸發條件。"""
    r = load_report()
    if not r:
        return {"date": None, "offense": [], "defense": []}
    ordered = {o["code"] for o in r.get("orders", [])}
    if _INTRA["v"] is not None:  # 盤中計畫（每 40 分鐘重算）已列入的也排除
        ordered |= {o["code"] for o in _INTRA["v"].get("orders", [])}
    held = set(pfm.build_portfolio().positions)
    off, dfn = [], []
    for rank, x in enumerate(r.get("top_picks", []), 1):
        if x["code"] in ordered or x["code"] in held:
            continue
        hot = x.get("signal") == "過熱不追"
        why = (f"{x['overheat']}" if hot else
               f"進攻分數 {x['score']:+.2f} 排第 {rank} 名，今天因檔數與資金上限未列入")
        trigger = ("等 RSI 降回 82 以下或回檔整理後重新評估；每天盤前會自動重算" if hot else
                   "前面標的被停損、出清或有新資金時優先遞補；分數維持在門檻以上就可買")
        off.append({"code": x["code"], "name": x["name"], "sector": x.get("sector"), "basket": "offense",
                    "kind": "過熱等回檔" if hot else "候補", "rank": rank, "score": x["score"],
                    "p_newhigh": x.get("p_newhigh"), "exp_excess": x.get("exp_excess"), "dist_hi": x.get("dist_hi"),
                    "rev_yoy": x.get("rev_yoy"), "why_not_yet": why, "trigger": trigger})
    for rank, x in enumerate(r.get("def_picks", []), 1):
        if x["code"] in ordered or x["code"] in held:
            continue
        dfn.append({"code": x["code"], "name": x["name"], "sector": x.get("sector"), "basket": "defense",
                    "kind": "候補", "rank": rank, "score": x["def_score"], "p_up": x.get("p_up"),
                    "exp_dd": x.get("exp_dd"), "beta": x.get("beta"),
                    "why_not_yet": f"穩健分數 {x['def_score']:+.2f} 排第 {rank} 名，今天因檔數與資金上限未列入",
                    "trigger": "穩健籃有持股出清或比例調高時優先遞補"})
    etf_list = []
    try:  # ETF 候補：一個月模型收正機率高、尚未列入下單者（含溢價不追）
        from src.agents import etf as etf_agent

        ctx = on_demand.context()
        ap = ctx["alpha"]["predictions"]
        prem = etf_agent.premiums()
        cfg = params.basket("etf")
        for rank, c in enumerate(sorted([c for c in ap if c in ctx["etf"]], key=lambda c: -ap[c].get("def_combo", 0)), 1):
            if c in ordered or c in held:
                continue
            name = stock_meta(c)["name"]
            pr = (prem.get(c) or {}).get("premium_pct")
            hot = pr is not None and pr > cfg["max_premium_pct"]
            etf_list.append({"code": c, "name": name, "basket": "etf", "kind": "溢價不追" if hot else "候補", "rank": rank,
                             "score": ap[c].get("def_combo"), "p_up": ap[c].get("p_up"), "exp_dd": ap[c].get("exp_dd"),
                             "category": etf_agent.category(c, name), "premium_pct": pr,
                             "why_not_yet": (f"溢價 {pr:+.2f}% 超過 {cfg['max_premium_pct']}%" if hot else
                                             f"ETF 穩健排名第 {rank}，今天 ETF 籃已有更高分或同類別的 ETF"),
                             "trigger": "溢價收斂到 1% 以內再評估" if hot else "ETF 籃有出清或比例調高時遞補（同類別最多 1 檔）"})
            if len(etf_list) >= limit:
                break
    except Exception as e:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning("ETF 潛在標的失敗：%s", e)
    return {"date": r["date"], "target_date": r.get("target_date"), "offense": off[:limit], "defense": dfn[:limit], "etf": etf_list,
            "note": "潛在標的不是今天的下單建議；要買之前請先用個股分析或問 AI 助理取得當下的張數與限價"}


def today_status(code: str, analysis: dict) -> dict:
    """這檔股票在今天建議中的位置：有沒有列入、排第幾、和最後一檔入選的差距、拖累分數的項目。"""
    from src.agents.strategy import COMPONENT_NAMES, DEF_NAMES

    r = load_report() or {}
    orders = r.get("orders", [])
    if _INTRA["v"] is not None:
        orders = orders + [o for o in _INTRA["v"].get("orders", []) if o["code"] not in {x["code"] for x in orders}]
    if any(o["code"] == code for o in orders):
        return {"in_today_orders": True, "note": "已列入今天的下單建議"}
    off_rank = [x["code"] for x in r.get("ranking", [])]
    def_rank = [x["code"] for x in r.get("def_picks", [])]
    off_sel = [o for o in orders if o.get("action") == "BUY" and o.get("basket") == "offense"]
    def_sel = [o for o in orders if o.get("action") == "BUY" and o.get("basket") == "defense"]
    off, dfn = analysis.get("offense") or {}, analysis.get("defense") or {}
    drag = sorted(((v, COMPONENT_NAMES.get(k, k)) for k, v in (off.get("components") or {}).items()), key=lambda t: t[0])[:2]
    ddrag = sorted(((v, DEF_NAMES.get(k, k)) for k, v in (dfn.get("components") or {}).items()), key=lambda t: t[0])[:2]
    return {
        "in_today_orders": False, "report_date": r.get("date"),
        "offense": {"score": off.get("score"), "signal": off.get("signal"),
                    "rank_in_candidates": off_rank.index(code) + 1 if code in off_rank else None,
                    "candidates": len(off_rank), "selected_count": len(off_sel),
                    "lowest_selected_score": min((o["score"] for o in off_sel if o.get("score") is not None), default=None),
                    "weakest_components": [f"{n} {v:+.2f}" for v, n in drag]},
        "defense": {"score": dfn.get("score"), "signal": dfn.get("signal"), "eligible": dfn.get("eligible"),
                    "rank_in_picks": def_rank.index(code) + 1 if code in def_rank else None,
                    "selected_count": len(def_sel),
                    "lowest_selected_score": min((o["score"] for o in def_sel if o.get("score") is not None), default=None),
                    "weakest_components": [f"{n} {v:+.2f}" for v, n in ddrag]},
        "note": "分數元件範圍 -1～+1；每天依綜合分數由高到低取前幾名，分數低於最後一檔入選者就不會列入",
    }


def etf_view() -> dict:
    """ETF 籃：配置比例與理由、候選 ETF（類別、分數、折溢價、建議股數）。"""
    from src.agents import on_demand

    lp = live_intraday()
    a = lp.get("allocation") or {}
    orders = [o for o in lp.get("orders", []) if o.get("basket") == "etf"]
    ctx = on_demand.context()
    from src.agents import etf as etf_agent
    from src.config import stock_meta

    prem = etf_agent.premiums()
    a_preds = ctx["alpha"]["predictions"]
    cands = sorted([c for c in a_preds if c in ctx["etf"]], key=lambda c: -a_preds[c].get("p_up", 0))[:10]
    share = a.get("etf")
    return {"today_etf_share_pct": round(share * 100) if share is not None else None,
            "base_etf_share_pct": round(params.allocation()["etf"]["base"] * 100),
            "note": "回答比例時一律使用 today_etf_share_pct（今日實際比例），base 只是調整前的基準",
            "etf_reasons": a.get("etf_reasons"),
            "planned_orders": [{k: o.get(k) for k in ("code", "name", "planned_shares", "limit_price", "status", "advice")}
                               for o in orders],
            "candidates": [{"code": c, "name": stock_meta(c)["name"],
                            "category": etf_agent.category(c, stock_meta(c)["name"]),
                            "p_up": round(a_preds[c].get("p_up", 0), 3), "exp_dd": round(a_preds[c].get("exp_dd", 0), 4),
                            "premium_pct": (prem.get(c) or {}).get("premium_pct")} for c in cands]}


def futures_view() -> dict:
    lp = live_intraday()
    return {"derivatives": lp.get("derivatives"), "funding": lp.get("funding")}


FUTURES_INFO = {
    "TX": {"name": "臺股期貨（大台）", "aliases": ("台指期", "臺指期", "大台", "臺股期貨", "台股期貨", "TX", "TXF", "台指")},
    "MTX": {"name": "小型臺指（小台）", "aliases": ("小台", "小型台指", "小型臺指", "MTX", "MXF")},
    "TMF": {"name": "微型臺指（微台）", "aliases": ("微台", "微型台指", "微型臺指", "TMF")},
    "TXO": {"name": "臺指選擇權", "aliases": ("選擇權", "台指選", "臺指選", "TXO", "賣權", "買權", "PUT", "CALL")},
}


def futures_search(q: str) -> list[dict]:
    q = q.strip().upper()
    out = []
    for root, info in FUTURES_INFO.items():
        if q and any(q in a.upper() or a.upper() in q for a in info["aliases"]):
            out.append({"code": root, "name": info["name"], "sector": "期貨" if root != "TXO" else "選擇權",
                        "kind": "futures"})
    return out


def _ma(vals: list[float], n: int, i: int):
    return round(sum(vals[i - n + 1:i + 1]) / n, 1) if i >= n - 1 else None


def futures_kline(root: str, days: int = 260) -> dict:
    """期貨近月連續日 K（期交所歷史行情；選擇權用台指期近月代表標的走勢）＋建議價位線＋成交紀錄。"""
    from src.agents import futures_data

    base = "TX" if root == "TXO" else root
    hist = futures_data.history(base, max(days, 70))
    closes = [h["close"] for h in hist]
    candles = [{**{k: h[k] for k in ("time", "open", "high", "low", "close")}, "volume": int(h["volume"]),
                "ma5": _ma(closes, 5, i), "ma20": _ma(closes, 20, i), "ma60": _ma(closes, 60, i), "inst": None}
               for i, h in enumerate(hist)][-days:]
    trades = [t for t in pfm.load_trades() if str(t.get("code", "")).upper() == root]
    markers = [{"time": t["date"], "side": t["side"], "shares": int(float(t["shares"])), "price": float(t["price"])} for t in trades]
    lines = []
    d = (_INTRA["v"] or {}).get("derivatives") or {}
    for o in d.get("orders", []):
        if o["code"] == base or root in ("TX", o["code"]):
            lines += [{"label": f"建議進場 {o['contract']}", "price": o["limit_price"], "kind": "entry"},
                      {"label": "停損", "price": o["stop"], "kind": "stop"},
                      {"label": "停利", "price": o["take_profit"], "kind": "tp"}]
    if root == "TXO" and d.get("hedge"):
        lines.append({"label": f"賣權履約價 {int(d['hedge']['strike'])}（開始有保護）", "price": d["hedge"]["strike"], "kind": "stop"})
    return {"code": root, "name": FUTURES_INFO.get(root, {}).get("name", root), "candles": candles, "markers": markers,
            "lines": lines, "source": "期交所期貨每日交易行情（近月合約連續；每日取成交量最大的月合約）"
            + ("；選擇權以台指期近月代表標的走勢" if root == "TXO" else "")}


def futures_detail(root: str) -> dict:
    from src.agents import futures_data as fd
    from src.config import rules as _rules

    root = root.upper()
    spec = _rules()["futures"]["contracts"].get(root, {})
    margins = fd.margins()
    out = {"root": root, "name": FUTURES_INFO.get(root, {}).get("name", root), "multiplier": spec.get("multiplier"),
           "tax": "股價類期貨交易稅 0.002%" if root != "TXO" else "選擇權交易稅 0.01%（競賽規則）",
           "commission_assumed": _rules()["futures"]["commission_per_contract"].get(root),
           "trading_hours": "08:45～13:45（競賽平台接受前一日 15:30 後預掛）"}
    if root == "TXO":
        months = [fd.front_month(), fd.front_month(min_days=40)]
        spot = fd.futures_quotes("TX").get("spot")
        chains = {}
        for ym in dict.fromkeys(months):
            ch = fd.put_chain(ym)
            if spot:
                ch = [r for r in ch if spot * 0.88 <= r["strike"] <= spot * 1.02]
            chains[ym] = [{**r, "dist_pct": round((r["strike"] / spot - 1) * 100, 2) if spot else None,
                           "premium_per_lot": round(r["price"] * 50) if r.get("price") else None} for r in ch]
        out.update(spot=spot, months=[{"ym": ym, "settlement": str(fd.settlement_date(ym)),
                                       "days_left": fd.days_to_settlement(ym)} for ym in chains],
                   puts=chains, note="只做買方：買進賣權的最大損失＝權利金；價外越多越便宜、保護越晚開始")
    else:
        q = fd.futures_quotes(root)
        rows = []
        for ym, m in sorted(q["months"].items())[:4]:
            rows.append({"ym": ym, "contract": f"{root}{ym}", **m, "settlement": str(fd.settlement_date(ym)),
                         "days_left": fd.days_to_settlement(ym),
                         "basis": round(m["price"] - q["spot"], 1) if q.get("spot") and m.get("price") else None})
        mg = margins.get(root, {})
        front = rows[0] if rows else {}
        out.update(spot=q.get("spot"), source=q.get("source"), months=rows, margin=mg,
                   notional=round(front["price"] * spec["multiplier"]) if front.get("price") else None,
                   leverage=round(front["price"] * spec["multiplier"] / mg["initial"], 1) if front.get("price") and mg.get("initial") else None)
    plan = (_INTRA["v"] or {}).get("derivatives")
    out["system_view"] = plan
    return out


def weekly_report(week: str | None = None) -> dict:
    """產生指定一週（任一天即可）的週報 PPT，存到 reports/weekly/，回傳下載路徑。"""
    import datetime as dt

    from src.agents import weekly
    from src.config import REPORT_DIR, now_tw
    from src.reports import weekly_pptx

    day = dt.date.fromisoformat(week) if week else now_tw().date()
    w = weekly.collect(day)
    text, verified = weekly.narrate(w, weekly.next_week_plan())
    name = f"{w['week_label'].replace(' ', '')}週報_{w['monday']}.pptx"
    path = weekly_pptx.build(w, text, verified, REPORT_DIR / "weekly" / name)
    from pptx import Presentation

    return {"file": f"weekly/{name}", "name": name, "week_no": w["week_no"], "week_label": w["week_label"], "period": f"{w['monday']}～{w['end_day']}",
            "slides": len(Presentation(str(path)).slides), "trades": w["trade_count_week"], "verified_text": verified,
            "headline": text.headline}


def weekly_reports() -> list[dict]:
    from src.config import REPORT_DIR

    out = []
    for fp in sorted((REPORT_DIR / "weekly").glob("*.pptx"), key=lambda p: p.stat().st_mtime, reverse=True):
        import datetime as dt

        out.append({"file": f"weekly/{fp.name}", "name": fp.name,
                    "generated_at": dt.datetime.fromtimestamp(fp.stat().st_mtime).strftime("%m/%d %H:%M")})
    return out


def competition_weeks() -> list[dict]:
    """比賽每一週的週一～週五（給前端選週）。"""
    import datetime as dt

    from src.config import rules as _rules

    c = _rules()["competition"]
    start, end = dt.date.fromisoformat(str(c["start"])), dt.date.fromisoformat(str(c["end"]))
    mon = start - dt.timedelta(days=start.weekday())
    out, n = [], 1
    while mon <= end:
        out.append({"week_no": n, "monday": mon.isoformat(), "friday": (mon + dt.timedelta(days=4)).isoformat()})
        mon += dt.timedelta(days=7)
        n += 1
    return out


# ---------------------------------------------------------------- 系統健康
_HEALTH: dict = {"t": 0.0, "runs": None}
WORKFLOWS = {"invest-agent.yml": "每日排程（盤前／盤中／盤後／週報）", "keepalive.yml": "網站保持清醒", "deploy-space.yml": "網站部署"}


def _gh() -> tuple[str, dict] | None:
    import os

    repo = os.getenv("GITHUB_REPO") or "ZHANGFU-XIANG/xq"
    tok = os.getenv("GITHUB_TOKEN") or os.getenv("GH_PAT")
    if not tok:
        return None
    return repo, {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}


def _workflow_runs() -> dict:
    """各 workflow 最近的執行（5 分鐘快取）。"""
    import time

    import requests

    if _HEALTH["runs"] is not None and time.time() - _HEALTH["t"] < 300:
        return _HEALTH["runs"]
    out = {}
    gh = _gh()
    if gh:
        repo, h = gh
        for wf, label in WORKFLOWS.items():
            try:
                runs = requests.get(f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/runs?per_page=15",
                                    headers=h, timeout=20).json().get("workflow_runs", [])
            except Exception as e:  # noqa: BLE001
                out[wf] = {"label": label, "error": str(e)[:120], "runs": []}
                continue
            out[wf] = {"label": label, "runs": [{"time": r["created_at"], "event": r["event"], "status": r["status"],
                                                  "conclusion": r["conclusion"], "url": r["html_url"]} for r in runs]}
    _HEALTH.update(t=time.time(), runs=out)
    return out


def health() -> dict:
    """排程是否照時間執行、資料是否新鮮；異常會列出原因與處理方式。"""
    import datetime as dt

    from src import config
    from src.agents import intraday
    from src.api import sync
    from src.data import providers

    now = config.now_tw()
    today = now.date()
    trading = config.is_trading_day(today)
    hm = now.hour * 60 + now.minute
    checks = []

    def add(name: str, status: str, detail: str, fix: str = "") -> None:
        checks.append({"name": name, "status": status, "detail": detail, "fix": fix})

    def tw(iso: str) -> dt.datetime:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(config.TW_TZ)

    runs = _workflow_runs()
    for wf, info in runs.items():
        rs = info.get("runs") or []
        if info.get("error"):
            add(info["label"], "unknown", f"無法讀取 GitHub：{info['error']}")
            continue
        if not rs:
            add(info["label"], "warn" if wf != "invest-agent.yml" else "unknown", "還沒有執行紀錄")
            continue
        last = rs[0]
        ok = next((r for r in rs if r["conclusion"] == "success"), None)
        last_t = tw(last["time"]).strftime("%m/%d %H:%M")
        if last["status"] != "completed":
            add(info["label"], "running", f"執行中（{last_t} 開始）")
        elif last["conclusion"] == "success":
            add(info["label"], "ok", f"最近一次 {last_t} 成功")
        else:
            add(info["label"], "error", f"最近一次 {last_t} {last['conclusion']}" +
                (f"；上次成功 {tw(ok['time']).strftime('%m/%d %H:%M')}" if ok else "；尚無成功紀錄"),
                "到 GitHub Actions 看錯誤訊息，或請 AI 助理重新執行")
    if "keepalive.yml" in runs:
        rs = [r for r in runs["keepalive.yml"].get("runs", []) if r["event"] == "schedule"]
        if not rs:
            add("保持清醒排程", "warn", "每 6 小時的排程還沒有自動觸發過（GitHub 新排程有時會延遲幾小時）")

    # 資料新鮮度
    latest = load_report() or {}
    if trading and hm >= 7 * 60 + 40:
        if latest.get("target_date") == today.isoformat():
            add("今天的盤前報告", "ok", f"{latest.get('generated_at')} 產生")
        else:
            add("今天的盤前報告", "error", f"還沒有 {today} 的盤前報告（07:00 應產生）", "請 AI 助理「重新執行盤前報告」")
    else:
        add("最新報告", "ok" if latest else "warn", f"{latest.get('date', '無')} {latest.get('title', '')}（{latest.get('generated_at', '—')}）")
    files = sorted((config.DATA_DIR / "intraday").glob("*.json"))
    if files:
        f = files[-1].stem  # 20261005_0940
        last = dt.datetime.strptime(f, "%Y%m%d_%H%M").replace(tzinfo=config.TW_TZ)
        age = (now - last).total_seconds() / 60
        stale = trading and 9 * 60 + 10 <= hm <= 13 * 60 + 40 and age > 70
        add("雲端盤中建議", "error" if stale else "ok", f"最近一次 {last.strftime('%m/%d %H:%M')}（{age:.0f} 分鐘前）",
            "盤中每 40 分鐘一輪，超過 70 分鐘沒更新代表排程延遲或失敗" if stale else "")
    elif trading and hm >= 9 * 60 + 10:
        add("雲端盤中建議", "warn", "今天還沒有雲端盤中建議紀錄")
    if sync.cloud():
        st = sync.state
        ls = st.get("last_sync")
        ls = dt.datetime.fromtimestamp(ls, config.TW_TZ).strftime("%m/%d %H:%M") if isinstance(ls, (int, float)) else (ls or "尚未")
        add("與 GitHub 同步", "error" if st.get("error") else "ok", f"最近一次 {ls}" +
            (f"；錯誤：{st['error']}" if st.get("error") else ""))
    ps = providers.status()
    add("FinMind 資料來源", "ok" if ps["finmind_available"] else "warn",
        f"{ps['finmind_tokens']} 組 token" + ("；全部冷卻中，暫用官方資料" if not ps["finmind_available"] else ""))
    order = {"error": 0, "warn": 1, "unknown": 2, "running": 3, "ok": 4}
    overall = min((c["status"] for c in checks), key=lambda s: order.get(s, 9), default="ok")
    return {"time": now.strftime("%Y-%m-%d %H:%M"), "overall": overall, "checks": checks,
            "slot": intraday.current_slot(now),
            "runs": {wf: {"label": v["label"], "runs": v.get("runs", [])[:8]} for wf, v in runs.items()}}
