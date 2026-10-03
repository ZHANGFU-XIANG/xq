"""IntradayAgent：盤中每小時操作建議（交易日 09:05～13:05），由使用者親手下單。

以證交所即時報價檢查：
- 今日計畫買進：現價 vs 限價 → 可以下單／不追價維持掛單／漲停買不到／已成交
- 今日計畫賣出與持股警示：跌破停損、跌停、負面新聞 → 建議賣出股數與限價
- 收盤前持股率：每日收盤檢查 ≥70%（建倉期後未達每次扣 0.2%），只算實際成交 → 不足時提醒要補多少
全部規則化，不使用 LLM（不消耗額度、結果穩定）。
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from src.agents import alerts, sizer
from src.config import DATA_DIR, now_tw, rules, stock_meta
from src.state import params
from src.state import portfolio as pfm

log = logging.getLogger(__name__)
OUT_DIR = DATA_DIR / "intraday"


def _taiex_rt() -> dict | None:
    try:
        import requests

        r = requests.get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                         params={"ex_ch": "tse_t00.tw", "json": 1, "delay": 0}, timeout=15).json()
        m = r["msgArray"][0]
        last, prev = float(m.get("z") or m.get("y")), float(m["y"])
        return {"price": last, "chg_pct": round((last / prev - 1) * 100, 2), "time": m.get("t")}
    except Exception as e:  # noqa: BLE001
        log.warning("大盤即時失敗：%s", e)
        return None


def _order_status(o: dict, rt: dict, held: int) -> dict:
    code, limit = o["code"], o["limit_price"]
    q = rt.get(code) or {}
    price = q.get("price")
    base = {"code": code, "name": o["name"], "action": o["action"], "basket": o.get("basket"), "planned_shares": o["shares"],
            "limit_price": limit, "price": price, "chg_pct": q.get("chg_pct")}
    if o["action"] == "BUY":
        if held >= o["shares"] * 0.9:
            return {**base, "status": "已成交", "advice": f"持股已有 {held:,} 股，這筆已完成"}
        remain = o["shares"] - held
        if price is None:
            return {**base, "status": "無報價", "advice": "暫無即時報價，維持原掛單"}
        if q.get("at_limit_up"):
            return {**base, "status": "漲停買不到", "advice": "已漲停，不追價；今日若未成交，收盤後重新評估"}
        gap = (price / limit - 1) * 100
        if price <= limit:
            return {**base, "status": "可以下單", "remaining": remain,
                    "advice": f"現價 {price} ≤ 限價 {limit}：可以下單買進 {sizer.fmt_shares(remain)}（限價 {limit}）"}
        if gap <= 1.0:
            return {**base, "status": "接近限價", "remaining": remain,
                    "advice": f"現價 {price} 高於限價 {gap:.1f}%：維持限價 {limit} 掛單等回檔"}
        return {**base, "status": "不追價", "remaining": remain,
                "advice": f"現價 {price} 已高於限價 {gap:.1f}%：不追高，維持限價 {limit}；若收盤前仍未成交，明天依新報告調整"}
    # SELL
    if held == 0:
        return {**base, "status": "已成交", "advice": "已無持股，這筆已完成"}
    if price is None:
        return {**base, "status": "無報價", "advice": "暫無即時報價，維持原賣單"}
    if q.get("at_limit_down"):
        return {**base, "status": "跌停賣不掉", "advice": f"跌停鎖住，掛跌停價 {q.get('limit_down')} 排隊，打開時優先成交"}
    return {**base, "status": "可以下單", "advice": f"賣出 {sizer.fmt_shares(min(held, o['shares']))}，限價 {limit}（現價 {price}）"}


def run(report: dict | None) -> dict:
    """report：今日最新的每日報告 facts（含計畫訂單）。"""
    now = now_tw()
    pf = pfm.build_portfolio()
    meta = pfm.load_meta()
    orders = [o for o in (report or {}).get("orders", []) if (report or {}).get("target_date") == now.date().isoformat()]
    codes = list(dict.fromkeys(list(pf.positions) + [o["code"] for o in orders]))
    rt = alerts.realtime(codes)
    prices = {c: (rt.get(c) or {}).get("price") for c in pf.positions}
    from src.agents import market_data, on_demand

    for c in pf.positions:  # 無即時價時用最近收盤
        if not prices.get(c):
            df = on_demand.price_df(c)
            prices[c] = float(df.Close.iloc[-1]) if not df.empty else pf.positions[c].avg_cost
    nav = pf.nav(prices)
    ratio = pf.holding_ratio(prices)
    st = params.strategy()

    order_rows = [_order_status(o, rt, pf.positions[o["code"]].shares if o["code"] in pf.positions else 0) for o in orders]
    feats = {c: market_data.tw_features(c, on_demand.current_target()) for c in pf.positions}
    hold_alerts = alerts.holding_alerts(pf, meta, rt, {c: prices[c] for c in pf.positions}, feats,
                                        alerts.attention_lists(), alerts.news_risk({c: stock_meta(c)["name"] for c in pf.positions}))

    # 收盤前持股率（只算實際成交）
    min_ratio = rules()["requirements"]["min_holding_ratio"]
    build_end = dt.date.fromisoformat(str(rules()["competition"]["build_period_end"]))
    pending_buy = sum(r["remaining"] * (r["price"] or r["limit_price"]) for r in order_rows
                      if r["action"] == "BUY" and r.get("remaining"))
    ratio_note = None
    if ratio < min_ratio:
        need = (min_ratio - ratio) * nav
        penalty = now.date() > build_end
        ratio_note = (f"目前實際持股率 {ratio * 100:.1f}%，低於 {min_ratio * 100:.0f}%，還差約 {need:,.0f} 元；"
                      + ("收盤後會扣報酬率 0.2%，請在 13:30 前讓買單成交（可改用較積極的限價）"
                         if penalty else "目前仍在建倉期（不罰），但請盡快建立部位"))
    elif ratio < st["target_holding_ratio"] - 0.05:
        ratio_note = f"目前實際持股率 {ratio * 100:.1f}%，高於 70% 下限，但低於目標 {st['target_holding_ratio'] * 100:.0f}%"

    actions = []
    for a in hold_alerts:
        if a.get("sell_shares"):
            actions.append(f"【賣出】{a['title']}：{a['action']}（{a['reasons'][0]}）")
    for r in order_rows:
        if r["status"] in ("可以下單",):
            actions.append(f"【{'買進' if r['action'] == 'BUY' else '賣出'}】{r['code']} {r['name']}：{r['advice']}")
    if ratio_note and ratio < min_ratio:
        actions.append(f"【持股率】{ratio_note}")
    waits = [f"{r['code']} {r['name']}：{r['advice']}" for r in order_rows if r["status"] in ("接近限價", "不追價", "漲停買不到", "跌停賣不掉")]
    out = {
        "time": now.strftime("%Y-%m-%d %H:%M"), "is_last_check": now.hour >= 13,
        "taiex": _taiex_rt(), "nav": round(nav), "holding_ratio": round(ratio, 4),
        "return_pct": round((nav / rules()["capital"]["initial"] - 1) * 100, 3),
        "orders": order_rows, "holding_alerts": hold_alerts, "actions": actions, "waits": waits,
        "ratio_note": ratio_note, "pending_buy_value": round(pending_buy),
        "summary": (f"{len(actions)} 項需要動作" if actions else "目前不需要動作，維持原掛單"),
    }
    return out


def save(out: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = out["time"].replace("-", "").replace(" ", "_").replace(":", "")
    (OUT_DIR / f"{stamp}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def email_body(out: dict) -> str:
    tx = out.get("taiex") or {}
    lines = [f"盤中建議 {out['time']}" + ("（收盤前最後檢查）" if out["is_last_check"] else ""),
             f"加權指數 {tx.get('price', '—')}（{tx.get('chg_pct', 0):+.2f}%）｜總資產 {out['nav']:,} 元｜實際持股率 {out['holding_ratio'] * 100:.1f}%",
             "", "要做的事：" if out["actions"] else "要做的事：無，維持原掛單"]
    lines += [f"- {a}" for a in out["actions"]]
    if out["waits"]:
        lines += ["", "等待中："] + [f"- {w}" for w in out["waits"]]
    others = [a for a in out["holding_alerts"] if not a.get("sell_shares")]
    if others:
        lines += ["", "持股提醒："] + [f"- {a['title']}：{a['reasons'][0]} → {a['action']}" for a in others]
    if out["ratio_note"] and not any(a.startswith("【持股率】") for a in out["actions"]):
        lines += ["", out["ratio_note"]]
    return "\n".join(lines)
