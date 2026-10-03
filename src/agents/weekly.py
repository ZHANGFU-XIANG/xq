"""WeeklyReportAgent：彙整指定一週（週一～週五）的實際成交、每日市場狀況與事件、績效，產出週報資料。

資料來源（全部是系統紀錄，不由 LLM 產生數字）：
- 成交：data/trades.csv（AI 助理記錄、XQ 匯入同步）
- 每筆成交的理由：當天盤前／盤後報告（data/facts）中同一檔的建議理由；盤中建議紀錄（data/intraday）；
  使用者記錄時寫的備註。三者都沒有 → 明確標示「不在系統建議中（自行判斷）」，不替使用者編理由
- 每日市場：加權指數日 K、報告中的漲跌歸因、國際大事、個股事件、美股隔夜
- 總資產：用成交紀錄重播到每一天，以當天收盤價計算（期貨用期交所近月收盤）
LLM 只負責把上述事實寫成摘要句子，數字一律經 verifier 核對；不可用時改用固定格式文字。
"""
from __future__ import annotations

import datetime as dt
import json
import logging

import pandas as pd

from src.agents import market_data
from src.config import DATA_DIR, FACTS_DIR, is_trading_day, rules, stock_meta
from src.state import portfolio as pfm

log = logging.getLogger(__name__)
WEEKDAYS = "一二三四五"


def week_range(any_day: dt.date) -> tuple[dt.date, dt.date]:
    mon = any_day - dt.timedelta(days=any_day.weekday())
    return mon, mon + dt.timedelta(days=4)


def _facts_for(d: dt.date) -> list[dict]:
    out = []
    for fp in sorted(FACTS_DIR.glob(f"{d.isoformat()}_*.json")):
        try:
            out.append(json.loads(fp.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    # 預測目標日是 d 的盤前報告（前一天晚上或當天早上產生，檔名可能是前一天）
    for fp in sorted(FACTS_DIR.glob("*.json")):
        try:
            r = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if r.get("target_date") == d.isoformat() and r not in out:
            out.append(r)
    return out


def _intraday_for(d: dt.date) -> list[dict]:
    out = []
    for fp in sorted((DATA_DIR / "intraday").glob(f"{d:%Y%m%d}_*.json")):
        try:
            out.append(json.loads(fp.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    return out


def _close_on(code: str, d: dt.date) -> float | None:
    try:
        df = market_data.tw_price(code)
        df = df[df.index <= pd.Timestamp(d)]
        return float(df.Close.iloc[-1]) if len(df) else None
    except Exception:  # noqa: BLE001
        return None


def _fut_close_on(root: str, d: dt.date) -> float | None:
    from src.agents import futures_data

    hist = [h for h in futures_data.history(root, 40) if h["time"] <= d.isoformat()]
    return hist[-1]["close"] if hist else None


def _prices_on(pf: pfm.Portfolio, d: dt.date) -> dict:
    prices = {c: p for c in pf.positions if (p := _close_on(c, d)) is not None}
    for c, dv in pf.derivs.items():
        if dv.kind == "FUT" and (p := _fut_close_on(dv.root, d)) is not None:
            prices[c] = p
    return prices


def _reason_for(t: dict, facts: list[dict], intra: list[dict]) -> dict:
    """這筆成交的理由：系統建議 → 盤中建議 → 使用者備註 → 標示自行判斷。"""
    code, side = t["code"], t["side"]
    act = "BUY" if side == "BUY" else "SELL"
    for r in facts:
        for o in r.get("orders", []):
            if o.get("code") == code and o.get("action") == act:
                why = (o.get("why_stock") or [])[:3] if act == "BUY" else [o.get("why_sell", "")]
                return {"source": f"{r['date']} {r.get('title', '報告')}建議", "basket": o.get("basket_name") or o.get("basket"),
                        "reasons": [w for w in why if w], "plan_price": o.get("limit_price"), "plan_shares": o.get("shares"),
                        "stop": o.get("stop"), "exit_plan": (o.get("exit_plan") or [])[:2], "risks": (o.get("risks") or [])[:2]}
    for s in reversed(intra):
        for o in s.get("orders", []):
            if o.get("code") == code and o.get("action") == act:
                return {"source": f"{s.get('time', '')} 盤中建議", "basket": o.get("basket"),
                        "reasons": [x for x in (o.get("why"), o.get("advice")) if x], "plan_price": o.get("limit_price"),
                        "plan_shares": o.get("planned_shares"), "stop": o.get("stop"), "exit_plan": [], "risks": []}
    note = (t.get("note") or "").strip()
    if note and note not in ("AI 助理記錄", "資金調撥") and not note.startswith("XQ"):
        return {"source": "記錄時的備註", "reasons": [note], "exit_plan": [], "risks": []}
    return {"source": "不在系統建議中（自行判斷）", "reasons": [], "exit_plan": [], "risks": []}


def _traded(d: dt.date, tx: pd.DataFrame) -> bool:
    """以加權指數當天有沒有資料為準（假日表只涵蓋比賽期間）；今天盤中還沒有日 K 時用交易日曆。"""
    if pd.Timestamp(d) in tx.index:
        return True
    return d >= dt.date.today() and is_trading_day(d)


def _day_market(d: dt.date, facts: list[dict], tx: pd.DataFrame) -> dict:
    row = tx[tx.index <= pd.Timestamp(d)].tail(2)
    chg = float(row.Close.iloc[-1] / row.Close.iloc[0] - 1) * 100 if len(row) == 2 and row.index[-1].date() == d else None
    close = float(row.Close.iloc[-1]) if len(row) and row.index[-1].date() == d else None
    events, macro, us = [], [], []
    for r in facts:
        events += [{"event": e["event"], "sentiment": e.get("sentiment"), "codes": e.get("codes", [])} for e in r.get("events", [])[:6]]
        macro += [{"event": m["event"], "factor": m.get("factor_name"), "impact_pct_nav": m.get("total_pct_nav")} for m in r.get("macro", [])[:4]]
        us = us or [{"name": u["name"], "change_pct": u["change_pct"]} for u in r.get("us_snapshot", [])[:6]]
    seen, uniq = set(), []
    for e in events:
        if e["event"] not in seen:
            seen.add(e["event"])
            uniq.append(e)
    return {"date": d.isoformat(), "weekday": WEEKDAYS[d.weekday()], "trading": _traded(d, tx), "taiex_close": close,
            "taiex_chg_pct": round(chg, 2) if chg is not None else None, "events": uniq[:5], "macro": macro[:3], "us": us,
            "market_view": next((r.get("market_view") for r in facts if r.get("market_view")), None)}


def collect(any_day: dt.date) -> dict:
    mon, fri = week_range(any_day)
    days = [mon + dt.timedelta(days=i) for i in range(5)]
    all_trades = pfm.load_trades()
    tx = market_data.taiex(1)
    initial = rules()["capital"]["initial"]

    def nav_at(d: dt.date) -> tuple[float, float, pfm.Portfolio]:
        pf = pfm.build_portfolio([t for t in all_trades if t["date"] <= d.isoformat()])
        prices = _prices_on(pf, d)
        return pf.nav(prices), pf.holding_ratio(prices), pf

    start_day = mon - dt.timedelta(days=1)
    nav0 = nav_at(start_day)[0] if any(t["date"] <= start_day.isoformat() for t in all_trades) else float(initial)
    daily, trades = [], []
    tx0 = tx[tx.index < pd.Timestamp(mon)].Close
    tx_base = float(tx0.iloc[-1]) if len(tx0) else None
    for d in days:
        facts = _facts_for(d)
        intra = _intraday_for(d)
        mkt = _day_market(d, facts, tx)
        day_trades = [t for t in all_trades if t["date"] == d.isoformat() and t["code"].upper() != "TRANSFER"]
        for t in day_trades:
            asset = pfm.asset_of(t)
            qty, price = int(float(t["shares"])), float(t["price"])
            mult = pfm.deriv_spec(t["code"].upper())["multiplier"] if asset in ("FUT", "OPT") else 1
            name = (t.get("contract") or t["code"]) if asset in ("FUT", "OPT") else stock_meta(t["code"])["name"]
            trades.append({"date": d.isoformat(), "weekday": WEEKDAYS[d.weekday()], "code": t["code"], "name": name,
                           "asset": asset, "side": t["side"], "qty": qty, "unit": "口" if asset in ("FUT", "OPT") else "股",
                           "price": price, "amount": round(qty * price * mult), "market": mkt,
                           "reason": _reason_for(t, facts, intra)})
        if mkt["trading"] and d <= dt.date.today():
            nav, ratio, _ = nav_at(d)
            daily.append({**mkt, "nav": round(nav), "holding_ratio": round(ratio, 4),
                          "taiex_idx": round(mkt["taiex_close"] / tx_base * 100, 2) if mkt["taiex_close"] and tx_base else None})
        else:
            daily.append({**mkt, "nav": None, "holding_ratio": None, "taiex_idx": None})
    last = next((x for x in reversed(daily) if x["nav"] is not None), None)
    end_day = dt.date.fromisoformat(last["date"]) if last else fri
    nav1, ratio1, pf1 = nav_at(end_day)
    prices1 = _prices_on(pf1, end_day)
    holdings = []
    for c, p in pf1.positions.items():
        px = prices1.get(c, p.avg_cost)
        holdings.append({"code": c, "name": stock_meta(c)["name"], "asset": "ETF" if stock_meta(c).get("is_etf") else "STOCK",
                         "shares": p.shares, "avg_cost": round(p.avg_cost, 2), "price": px,
                         "value": round(p.shares * px), "pnl": round(p.shares * px - p.cost_basis),
                         "pnl_pct": round((px / p.avg_cost - 1) * 100, 2) if p.avg_cost else 0,
                         "weight_pct": round(p.shares * px / nav1 * 100, 2) if nav1 else 0})
    for c, dv in pf1.derivs.items():
        px = prices1.get(c)
        holdings.append({"code": c, "name": c, "asset": "FUT" if dv.kind == "FUT" else "OPT", "shares": dv.qty,
                         "avg_cost": dv.avg_price, "price": px, "value": round(dv.value(px)), "pnl": round(dv.value(px)) if dv.kind == "FUT" else None,
                         "pnl_pct": None, "weight_pct": None})
    holdings.sort(key=lambda h: -(h["value"] or 0))
    # 本週各檔損益貢獻（期末市值＋本週賣出 − 期初市值 − 本週買進）
    _, _, pf0 = nav_at(start_day)
    p0 = _prices_on(pf0, start_day)
    contrib = {}
    for c in set(pf0.positions) | set(pf1.positions) | {t["code"] for t in trades if t["asset"] in ("STOCK", "ETF")}:
        v0 = pf0.positions[c].shares * p0.get(c, pf0.positions[c].avg_cost) if c in pf0.positions else 0
        v1 = pf1.positions[c].shares * prices1.get(c, pf1.positions[c].avg_cost) if c in pf1.positions else 0
        flow = sum((-1 if t["side"] == "BUY" else 1) * t["amount"] for t in trades if t["code"] == c)
        contrib[c] = round(v1 - v0 + flow)
    contrib_rows = sorted(({"code": c, "name": stock_meta(c)["name"], "pnl": v} for c, v in contrib.items() if v),
                          key=lambda r: -abs(r["pnl"]))[:10]
    tx_week = [x["taiex_chg_pct"] for x in daily if x["taiex_chg_pct"] is not None]
    tx_ret = None
    if tx_base and last and last["taiex_close"]:
        tx_ret = round((last["taiex_close"] / tx_base - 1) * 100, 2)
    start = dt.date.fromisoformat(str(rules()["competition"]["start"]))
    week_no = (mon - (start - dt.timedelta(days=start.weekday()))).days // 7 + 1
    week_no = max(0, week_no)  # 0 ＝ 比賽開始前（練習週）
    return {
        "week_no": week_no, "week_label": f"第 {week_no} 週" if week_no else "練習週", "monday": mon.isoformat(), "friday": fri.isoformat(), "end_day": end_day.isoformat(),
        "nav_start": round(nav0), "nav_end": round(nav1), "week_ret_pct": round((nav1 / nav0 - 1) * 100, 2) if nav0 else None,
        "cum_ret_pct": round((nav1 / initial - 1) * 100, 2), "holding_ratio": round(ratio1, 4),
        "taiex_week_pct": tx_ret, "taiex_days_up": sum(1 for x in tx_week if x > 0), "taiex_days_down": sum(1 for x in tx_week if x < 0),
        "trade_count_week": len(trades), "trade_count_total": pf1.trade_count, "symbols_total": len(pf1.traded_symbols),
        "cash": round(pf1.cash), "futures_equity": round(pf1.futures_equity(prices1)),
        "daily": daily, "trades": trades, "holdings": holdings, "contrib": contrib_rows,
    }


# ---------------------------------------------------------------- 文字（LLM 只寫句子，數字經核對）
from pydantic import BaseModel, Field  # noqa: E402


class TradeNote(BaseModel):
    code: str
    date: str
    comment: str = Field(description="這筆交易的理由與當時市場狀況，1～2 句，只能用提供的事實")


class WeeklyText(BaseModel):
    headline: str = Field(description="本週一句話結論（例如報酬與大盤比較、主要操作）")
    summary: list[str] = Field(description="本週重點 3 點")
    market_review: list[str] = Field(description="每個交易日一句：大盤漲跌與主要事件，格式「週X：…」")
    trade_notes: list[TradeNote]
    lessons: list[str] = Field(description="檢討 2～3 點：做對什麼、哪裡可改進，只根據事實")
    next_week: list[str] = Field(description="下週計畫 3 點，依提供的下週建議與規則提醒")


SYSTEM = ("你是投資競賽隊伍的報告撰寫者，用繁體中文、專業精簡、口語自然的句子寫週報。"
          "只能使用提供的 JSON 事實，任何數字都必須與事實一致，不可估算或編造；沒有的資料就不要寫。"
          "不要用粗體、井字號、表情符號或制式客套語。交易理由標示為「自行判斷」的，就寫自行判斷，不要替使用者編理由。")


def _facts_for_llm(w: dict, plan: dict) -> dict:
    return {"週次": w["week_no"], "期間": f"{w['monday']}～{w['end_day']}", "總資產期初": w["nav_start"], "總資產期末": w["nav_end"],
            "本週報酬%": w["week_ret_pct"], "累積報酬%": w["cum_ret_pct"], "加權指數本週%": w["taiex_week_pct"],
            "持股率%": round(w["holding_ratio"] * 100, 1), "本週成交筆數": w["trade_count_week"],
            "累計已成交筆數（門檻10）": w["trade_count_total"], "累計交易過標的數（門檻5）": w["symbols_total"],
            "每日": [{k: x[k] for k in ("date", "weekday", "taiex_chg_pct", "nav")} | {"事件": [e["event"] for e in x["events"][:3]],
                     "國際": [m["event"] for m in x["macro"][:2]]} for x in w["daily"] if x["trading"]],
            "成交": [{k: t[k] for k in ("date", "weekday", "code", "name", "asset", "side", "qty", "unit", "price", "amount")}
                     | {"理由來源": t["reason"]["source"], "理由": t["reason"]["reasons"][:2],
                        "當天大盤%": t["market"]["taiex_chg_pct"]} for t in w["trades"]],
            "期末持股": [{k: h[k] for k in ("code", "name", "shares", "pnl", "pnl_pct", "weight_pct")} for h in w["holdings"][:10]],
            "損益貢獻": w["contrib"], "下週": plan,
            "競賽門檻說明（不是每週）": ["總交易筆數：整個比賽期間累計 10 筆以上（買賣各算一筆）",
                                    "交易過的標的：第三週（10/19）起至少 5 檔；最後一天收盤仍須持有 5 檔以上",
                                    "持股率：每日收盤 ≥ 70%（10/16 前建倉期不罰），最後一天現金須 < 30%"]}


def next_week_plan() -> dict:
    """下週參考：最新報告的配置與建議、規則提醒（全部取自系統計算）。"""
    from src.api import data

    r = data.load_report() or {}
    lp = data._INTRA.get("v") or {}
    a = lp.get("allocation") or r.get("allocation") or {}
    pct = lambda v: None if v is None else round(v * 100)  # noqa: E731
    return {"下週目標配置%（佔股票＋ETF）": {"進攻籃": pct(a.get("offense")), "穩健籃": pct(a.get("defense")), "ETF籃": pct(a.get("etf"))},
            "建議買進": [f"{o['code']} {o['name']}" for o in (lp.get("orders") or r.get("orders") or []) if o.get("action") == "BUY"][:8],
            "期貨": (lp.get("derivatives") or {}).get("summary"),
            "尚未達標的規則": [c["item"] for c in r.get("compliance", []) if c.get("status") != "通過"][:4]}


def narrate(w: dict, plan: dict) -> tuple[WeeklyText, bool]:
    """回傳（文字, 是否由 LLM 產生且通過數字核對）。"""
    from src.agents.verifier import verify
    from src.llm.client import LLMError, get_client

    facts = _facts_for_llm(w, plan)
    client = get_client()
    if client.available():
        feedback = ""
        for _ in range(2):
            try:
                out = client.structured("事實（JSON）：\n" + json.dumps(facts, ensure_ascii=False, default=str) + feedback,
                                        WeeklyText, system=SYSTEM)
                text = " ".join([out.headline, *out.summary, *out.market_review, *(n.comment for n in out.trade_notes),
                                 *out.lessons, *out.next_week])
                ok, bad = verify(text, facts)
                if ok:
                    return out, True
                log.warning("週報文字有無法核對的數字：%s", bad)
                feedback = f"\n\n上一版有找不到出處的數字 {bad}，請只用事實中的數字重寫。"
            except LLMError as e:
                log.warning("週報文字產生失敗：%s", e)
                break
    return _template(w, plan), False


def _lessons(w: dict) -> list[str]:
    """固定格式檢討：全部由計算結果組成。"""
    out = []
    if w["week_ret_pct"] is not None and w["taiex_week_pct"] is not None:
        gap = w["week_ret_pct"] - w["taiex_week_pct"]
        out.append(f"總資產 {w['week_ret_pct']:+.2f}%，{'領先' if gap >= 0 else '落後'}加權指數 {abs(gap):.2f} 個百分點")
    if w["contrib"]:
        best = max(w["contrib"], key=lambda r: r["pnl"])
        worst = min(w["contrib"], key=lambda r: r["pnl"])
        if best["pnl"] > 0:
            out.append(f"最大貢獻：{best['code']} {best['name']} {best['pnl']:+,} 元")
        if worst["pnl"] < 0:
            out.append(f"最大拖累：{worst['code']} {worst['name']} {worst['pnl']:+,} 元")
    own = sum(1 for t in w["trades"] if t["reason"]["source"].startswith("不在系統建議"))
    if w["trades"]:
        out.append(f"本週 {len(w['trades'])} 筆成交中，{len(w['trades']) - own} 筆依系統建議、{own} 筆自行判斷")
    if w["holding_ratio"] < 0.7:
        out.append(f"期末持股率 {w['holding_ratio'] * 100:.1f}%，低於 70%（10/16 後每日扣報酬率 0.2%）")
    return out or ["本週沒有成交，也沒有持股變動"]


def _template(w: dict, plan: dict) -> WeeklyText:
    """LLM 不可用時的固定格式文字（只填入計算結果）。"""
    sign = lambda v: "—" if v is None else f"{v:+.2f}%"  # noqa: E731
    days = [x for x in w["daily"] if x["trading"]]
    return WeeklyText(
        headline=f"本週總資產 {sign(w['week_ret_pct'])}，加權指數 {sign(w['taiex_week_pct'])}，成交 {w['trade_count_week']} 筆",
        summary=[f"總資產由 {w['nav_start']:,} 元變為 {w['nav_end']:,} 元，累積報酬 {sign(w['cum_ret_pct'])}",
                 f"期末持股率 {w['holding_ratio'] * 100:.1f}%（規則下限 70%）",
                 f"累計成交 {w['trade_count_total']} 筆、交易過 {w['symbols_total']} 檔標的"],
        market_review=[f"週{x['weekday']}：加權 {sign(x['taiex_chg_pct'])}" + (f"；{x['events'][0]['event']}" if x["events"] else "")
                       for x in days],
        trade_notes=[TradeNote(code=t["code"], date=t["date"], comment="；".join(t["reason"]["reasons"][:2]) or t["reason"]["source"])
                     for t in w["trades"]],
        lessons=_lessons(w),
        next_week=[f"下週建議關注：{'、'.join(plan.get('建議買進', [])[:5]) or '依週一盤前報告'}",
                   f"期貨：{plan.get('期貨') or '依系統條件判斷'}"] + [f"留意規則：{x}" for x in plan.get("尚未達標的規則", [])[:2]],
    )
