"""OnDemandAgent：任一檔股票的即時分析（前端與 AI 助理使用）。

與每日報告共用 src/pipeline.py 的資料、模型與評分；所有數字由程式計算。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.parse

import feedparser
import numpy as np
import pandas as pd

from src import pipeline
from src.agents import attribution, compliance, macro, market_data, recovery, sizer, universe
from src.agents.news import _clean, _parse_time
from src.config import FACTS_DIR, TW_TZ, is_trading_day, next_trading_day, now_tw, rules, stock_meta
from src.state import params
from src.state import portfolio as pfm

log = logging.getLogger(__name__)


def current_target() -> dt.date:
    """分析用的目標交易日：交易日 14:00 前為今日（用前一日收盤資料），之後為下一個交易日。"""
    now = now_tw()
    today = now.date()
    if is_trading_day(today) and now.hour < 14:
        return today
    return next_trading_day(today)


def context() -> dict:
    return pipeline.market_context(current_target(), use_memory=True)


def resolve(query: str) -> list[dict]:
    """名稱或代號 → 候選股票（最多 10 筆）。"""
    q = query.strip().upper()
    info = universe.stock_info()
    if q in info:
        return [_brief(q)]
    hits = [c for c, m in info.items() if q in m["name"].upper() or c.startswith(q)]
    hits.sort(key=lambda c: (not c.startswith(q), len(info[c]["name"]), c))
    return [_brief(c) for c in hits[:10]]


def _brief(code: str) -> dict:
    m = stock_meta(code)
    return {"code": code, "name": m["name"], "sector": m["sector"], "market": m.get("market"), "is_etf": m.get("is_etf")}


def _f(x, nd=2):
    if x is None or (isinstance(x, float) and (np.isnan(x) or np.isinf(x))):
        return None
    return round(float(x), nd)


def price_df(code: str) -> pd.DataFrame:
    df = market_data.tw_price(code)
    return df[df.index < pd.Timestamp(current_target())] if df is not None and not df.empty else pd.DataFrame()


def profile(code: str) -> dict:
    """基本盤：價量、52 週高低、估值、月營收、法人、融資券、模型分數。"""
    context()
    df = price_df(code)
    if df.empty or len(df) < 2:
        return {"code": code, "error": "查無行情資料"}
    c = df.Close
    last, prev = df.iloc[-1], df.iloc[-2]
    hi52 = float(df.High.tail(252).max())
    lo52 = float(df.Low.tail(252).min())
    inst = market_data.tw_institutional(code)
    inst5 = None
    if not inst.empty:
        t5 = inst.tail(5)
        inst5 = {k: int(t5[k].sum() / 1000) for k in ("foreign", "trust", "dealer", "total")}
    a = context()["alpha"]["predictions"].get(code)
    return {
        **_brief(code), "date": str(df.index[-1].date()),
        "close": _f(last.Close), "open": _f(last.Open), "high": _f(last.High), "low": _f(last.Low),
        "change": _f(last.Close - prev.Close), "change_pct": _f((last.Close / prev.Close - 1) * 100),
        "volume_lots": int(last.Volume / 1000), "avg_volume20_lots": int(df.Volume.tail(20).mean() / 1000),
        "value_100m": _f(last.Close * last.Volume / 1e8),
        "high52": _f(hi52), "low52": _f(lo52), "dist_high52_pct": _f((last.Close / hi52 - 1) * 100),
        "ret20_pct": _f((c.iloc[-1] / c.iloc[-21] - 1) * 100) if len(c) > 21 else None,
        "ret60_pct": _f((c.iloc[-1] / c.iloc[-61] - 1) * 100) if len(c) > 61 else None,
        "valuation": market_data.tw_valuation(code), "revenue": market_data.tw_revenue(code) if not stock_meta(code)["is_etf"] else {},
        "institutional_5d_lots": inst5, "margin": market_data.tw_margin(code),
        "in_universe": a is not None,
        "model": {k: a[k] for k in ("p_newhigh", "exp_excess", "exp_ret", "p_up", "exp_dd", "alpha_rank", "def_combo",
                                    "eligible_def")} if a else None,
    }


def kline(code: str, days: int = 260) -> dict:
    df = price_df(code)
    if df.empty:
        return {"code": code, "candles": []}
    full = df.copy()
    for n in (5, 20, 60):
        full[f"ma{n}"] = full.Close.rolling(n).mean()
    view = full.tail(days)
    inst = market_data.tw_institutional(code)
    inst_map = {d.strftime("%Y-%m-%d"): int(v / 1000) for d, v in inst["total"].items()} if not inst.empty else {}
    candles = [{"time": d.strftime("%Y-%m-%d"), "open": _f(r.Open), "high": _f(r.High), "low": _f(r.Low), "close": _f(r.Close),
                "volume": int(r.Volume / 1000), "ma5": _f(r.ma5), "ma20": _f(r.ma20), "ma60": _f(r.ma60),
                "inst": inst_map.get(d.strftime("%Y-%m-%d"))} for d, r in view.iterrows()]
    markers = [{"time": t["date"], "side": t["side"], "shares": int(float(t["shares"])), "price": float(t["price"])}
               for t in pfm.load_trades() if t["code"] == code]
    lines = []
    latest = sorted(FACTS_DIR.glob("*.json"))
    for fp in reversed(latest):
        fx = json.loads(fp.read_text(encoding="utf-8"))
        o = next((o for o in fx.get("orders", []) if o["code"] == code and o["action"] == "BUY"), None)
        if o:
            lines = [{"label": f"建議限價（{fx['date']}）", "price": o["limit_price"], "kind": "entry"},
                     {"label": "停損", "price": o["stop"], "kind": "stop"}]
            tp = params.basket(o.get("basket", "offense"))["take_profit"]
            etf = stock_meta(code)["is_etf"]
            lines += [{"label": f"停利 +{lvl * 100:.0f}%", "price": sizer.round_tick(o["limit_price"] * (1 + lvl), etf), "kind": "tp"}
                      for lvl, _ in tp]
            break
    hi52 = float(df.High.tail(252).max())
    lines.append({"label": "52 週高點", "price": round(hi52, 2), "kind": "high"})
    return {"code": code, "name": stock_meta(code)["name"], "candles": candles, "markers": markers, "lines": lines}


def search_news(query: str, limit: int = 12) -> list[dict]:
    """個股或關鍵字新聞：Google 新聞 RSS（近 7 日）＋每日報告收錄的新聞。只回傳標題與連結，不經 LLM。"""
    out: list[dict] = []
    seen: set[str] = set()
    for fp in sorted(FACTS_DIR.glob("*.json"))[-5:][::-1]:
        for n in json.loads(fp.read_text(encoding="utf-8")).get("news", []):
            if query in n["title"] and n["title"] not in seen:
                seen.add(n["title"])
                out.append({**n, "date": fp.name[:10]})
    try:
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(query + " when:7d")
               + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
        for e in feedparser.parse(url).entries[:limit * 2]:
            title = _clean(e.get("title", ""))
            if title and title not in seen:
                seen.add(title)
                t = _parse_time(e)
                out.append({"source": "Google新聞", "title": title, "url": e.get("link", ""),
                            "time": t.astimezone(TW_TZ).strftime("%m/%d %H:%M") if t else ""})
    except Exception as e:  # noqa: BLE001
        log.warning("新聞搜尋失敗：%s", e)
    return out[:limit]


def _portfolio_state(ctx: dict, extra: list[str]):
    pf = pfm.build_portfolio()
    target = ctx["target"]
    sc = pipeline.score_codes(list(dict.fromkeys(list(pf.positions) + extra)), ctx, target)
    meta = pfm.load_meta()
    nav = pf.nav(sc["prices"])
    bo = pipeline.basket_of(pf, meta, sc["scores"])
    return pf, sc, meta, nav, bo


def analyze(code: str) -> dict:
    """完整個股分析與操作建議（含是否買、哪一籃、幾張、限價、停損、出場；持有中則給續抱/賣出建議）。"""
    ctx = context()
    target = ctx["target"]
    pf, sc, meta, nav, bo = _portfolio_state(ctx, [code])
    s = sc["scores"].get(code)
    if not s:
        return {"code": code, "error": "資料不足，無法分析（可能剛上市或已下市）"}
    today = now_tw().date()
    alloc = pipeline.current_allocation(ctx, target, today, nav)
    a = s.get("alpha")
    held = code in pf.positions
    out = {**_brief(code), "data_date": s["features"]["date"], "target_date": target.isoformat(),
           "in_universe": a is not None,
           "offense": {"score": s["score"], "signal": s["signal"], "components": s["components"],
                       "reasons": s["reasons"], "risks": s["risks"], "overheat": s.get("overheat")},
           "defense": {"score": s["def_score"], "signal": s["def_signal"], "eligible": s["eligible_def"],
                       "components": s["def_components"], "reasons": s["def_reasons"], "risks": s["def_risks"]},
           "model": {k: a[k] for k in ("p_newhigh", "exp_excess", "exp_ret", "p_up", "exp_dd", "hi252", "dist_hi")} if a else None,
           "overnight": sc["preds"].get(code), "revenue": s.get("revenue"),
           "allocation": {"offense": alloc["offense"], "defense": alloc["defense"]},
           "base_rates": {"newhigh": ctx["alpha"]["base_rate"], "up": ctx["alpha"]["up_base_rate"]}}
    if not a:
        out["warning"] = "不在流動性股票池內（20 日均成交值不足 5,000 萬或資料不足），一個月模型不適用，僅供技術面參考"

    # 今日漲跌歸因
    att = attribution.attribute({code: pf.positions[code].shares if held else 0}, ctx["panel"], ctx["taiex"], ctx["codes"],
                                pd.Timestamp(target), {r["ticker"]: r for r in sc["us_snap"]}, {})
    out["attribution"] = att["rows"][0] if att["rows"] else None
    sens = macro.sensitivities([code], ctx["panel"], ctx["us_hist"], pd.Timestamp(target)).get(code, {})
    names = {k: v["name"] for k, v in macro.settings()["macro_factors"].items()}
    out["macro_sensitivity"] = [{"factor": names[k], **v, "significant": abs(v["corr"]) >= macro.MIN_CORR}
                                for k, v in sens.items()]

    # 操作建議
    if held:
        pos = pf.positions[code]
        price = sc["prices"][code]
        rec = recovery.plan(pf, sc["prices"], sc["scores"], ctx["alpha"]["predictions"], meta, sc["ranked"], nav,
                            rules()["capital"]["initial"], {}, bo)
        sells = sizer.plan_sells(pf, sc["prices"], sc["feats"], sc["scores"], meta, nav, rec["actions"])
        mine = next((o for o in sells if o["code"] == code), None)
        item = next((i for i in rec["items"] if i["code"] == code), None)
        out["holding"] = {"shares": pos.shares, "avg_cost": round(pos.avg_cost, 2), "price": price,
                          "pnl": round(pos.shares * price - pos.cost_basis), "basket": params.basket(bo[code])["name"],
                          "stop": meta.get(code, {}).get("stop")}
        out["verdict"] = (f"賣出 {mine['shares']:,} 股：{mine['why_sell']}" if mine
                          else f"{item['decision']}：{item['why']}" if item else "續抱")
        out["sell_order"] = mine
        out["recovery"] = item
    else:
        if s["signal"] == "買進候選":
            basket = "offense"
        elif s["def_signal"] == "買進候選":
            basket = "defense"
        else:
            basket = "offense" if s["score"] >= s["def_score"] else "defense"
        order = sizer.size_single(s, basket, pf, sc["prices"], sc["feats"], nav, alloc, bo)
        bname = params.basket(basket)["name"]
        if s["signal"] == "過熱不追":
            out["verdict"] = f"過熱不追：{s['overheat']}；若要佈局，等回檔至試算限價附近再分批"
        elif s["signal"] == "買進候選" or s["def_signal"] == "買進候選":
            out["verdict"] = f"建議買進（{bname}）"
        elif s["score"] <= params.strategy()["sell_score_threshold"]:
            out["verdict"] = "偏空，不建議買進"
        else:
            out["verdict"] = f"觀望：兩籃分數都未達買進門檻（進攻 {s['score']:+.2f}／穩健 {s['def_score']:+.2f}）"
        out["buy_plan"] = order
    out["news"] = search_news(f"{stock_meta(code)['name']} {code}", 8)
    return out


def simulate(trades: list[dict]) -> dict:
    """假設執行一組買賣後的投資組合：現金、成本、持股率、雙籃比例與競賽規則檢查。不會寫入任何資料。"""
    ctx = context()
    pf0, sc, meta, nav0, bo = _portfolio_state(ctx, [t["code"] for t in trades])
    today = now_tw().date().isoformat()
    hyp = []
    for t in trades:
        price = t.get("price") or sc["prices"].get(t["code"])
        if not price:
            return {"error": f"{t['code']} 查無價格"}
        hyp.append({"date": today, "code": t["code"], "side": t["side"].upper(), "shares": int(t["shares"]),
                    "price": price, "margin": int(bool(t.get("margin")))})
    try:
        pf1 = pfm.build_portfolio(pfm.load_trades() + [{k: str(v) for k, v in h.items()} for h in hyp])
    except ValueError as e:
        return {"error": str(e)}
    prices = sc["prices"]
    nav1 = pf1.nav(prices)
    costs = sum(pfm.trade_cost(h["code"], h["side"], h["shares"], h["price"]) for h in hyp)
    for t in trades:
        if t["code"] not in bo:
            s = sc["scores"].get(t["code"], {})
            bo[t["code"]] = "defense" if s.get("def_signal") == "買進候選" and s.get("signal") != "買進候選" else "offense"
    mix = {"offense": 0.0, "defense": 0.0}
    for c, p in pf1.positions.items():
        mix[bo.get(c, "offense")] += p.shares * prices.get(c, 0)
    mv = sum(mix.values()) or 1
    checks = compliance.check(pf1, prices, [], dt.date.fromisoformat(today), nav1)
    return {"trades": hyp, "total_cost": round(costs), "nav_before": round(nav0), "nav_after": round(nav1),
            "cash_after": round(pf1.cash), "holding_ratio_before": round(pf0.holding_ratio(prices), 4),
            "holding_ratio_after": round(pf1.holding_ratio(prices), 4), "margin_loan_after": round(pf1.margin_loan()),
            "basket_mix_after": {params.basket(k)["name"]: round(v / mv, 4) for k, v in mix.items()},
            "positions_after": {c: p.shares for c, p in pf1.positions.items()},
            "trade_count_after": pf1.trade_count, "compliance": checks,
            "note": "試算以最近收盤價計算，未含盤中價格變動；只有實際成交才計入持股率"}
