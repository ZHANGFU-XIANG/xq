"""IntradayAgent：盤中每 40 分鐘重新計算操作建議（09:00、09:40、10:20、11:00、11:40、12:20、13:00），使用者親手下單。

每一輪：
1. 以證交所即時報價重算整份計畫（on_demand.live_plan）：持股 加碼／續抱／減碼／全部出清＋新部位建倉
   - 第一天沒有持股時就是「建倉建議」：依三籃比例、目標持股率 90% 配置
   - 盤中已漲 ≥7% 或漲停的不追，跌停的暫緩
2. 與早上盤前報告比較：新增、取消（附原因）、數量調整、與早盤相同
3. 每筆給出「現在能不能下單」：現價 ≤ 限價 → 可以下單；高於限價 → 掛單等待
4. 收盤前持股率檢查（只算實際成交；建倉期後 <70% 每次扣 0.2%）
全部規則化，不使用 LLM。
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from src.agents import sizer
from src.config import DATA_DIR, now_tw, rules
from src.state import params
from src.state import portfolio as pfm

log = logging.getLogger(__name__)
OUT_DIR = DATA_DIR / "intraday"
SLOTS = [(9, 0), (9, 40), (10, 20), (11, 0), (11, 40), (12, 20), (13, 0)]


def slot_label(slot: str) -> str:
    """「2026-10-04 休市」→「休市」；「2026-10-05 09:40」→「09:40」。"""
    return slot.split(" ", 1)[1] if " " in slot else slot


def market_open(slot: str) -> bool:
    return slot_label(slot) not in ("盤前", "休市", "收盤後")


def current_slot(now: dt.datetime | None = None) -> str:
    """目前所屬的 40 分鐘時段（建議在時段內固定，到下一個時段才更新）。"""
    from src.config import is_trading_day

    now = now or now_tw()
    if not is_trading_day(now.date()):
        return f"{now.date()} 休市"
    if (now.hour, now.minute) > (13, 30):
        return f"{now.date()} 收盤後"
    hm = (now.hour, now.minute)
    slot = None
    for s in SLOTS:
        if hm >= s:
            slot = s
    return f"{now.date()} {slot[0]:02d}:{slot[1]:02d}" if slot else f"{now.date()} 盤前"


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


def _order_status(o: dict, rt: dict, held: int, market_open: bool = True) -> dict:
    code, limit = o["code"], o["limit_price"]
    q = rt.get(code) or {}
    price = q.get("price")
    base = {"code": code, "name": o["name"], "action": o["action"], "basket": o.get("basket"), "asset": o.get("asset", "STOCK"),
            "planned_shares": o["shares"],
            "limit_price": limit, "price": price, "chg_pct": q.get("chg_pct"), "stop": o.get("stop"),
            "why": (o.get("why_sell") or (o.get("why_stock") or [""])[0])}
    odd = o["shares"] % 1000
    odd_note = f"（零股 {odd} 股要在盤中零股時段 09:10～13:30 另外下單）" if odd else ""
    if not market_open:  # 盤前／收盤後：可預掛單（平台接受前一日 15:30 後的預掛）
        verb = "買進" if o["action"] == "BUY" else "賣出"
        return {**base, "status": "可預掛單", "remaining": o["shares"],
                "advice": f"開盤前預掛：限價 {limit} {verb} {sizer.fmt_shares(o['shares'])}{odd_note}；開盤後每 40 分鐘依即時價重算"}
    if o["action"] == "BUY":
        if held >= o["shares"] * 0.9 and o.get("existing_before", 0) == 0:
            return {**base, "status": "已成交", "advice": f"持股已有 {held:,} 股，這筆已完成"}
        remain = o["shares"]
        if price is None:
            return {**base, "status": "無報價", "remaining": remain, "advice": f"暫無即時報價，掛限價 {limit} 買 {sizer.fmt_shares(remain)}"}
        if q.get("at_limit_up"):
            return {**base, "status": "漲停買不到", "advice": "已漲停，不追價；今日若未成交，收盤後重新評估"}
        gap = (price / limit - 1) * 100
        if price <= limit:
            return {**base, "status": "可以下單", "remaining": remain,
                    "advice": f"現價 {price} ≤ 限價 {limit}：可以下單買進 {sizer.fmt_shares(remain)}（限價 {limit}）"}
        if gap <= 1.0:
            return {**base, "status": "接近限價", "remaining": remain,
                    "advice": f"現價 {price} 高於限價 {gap:.1f}%：掛限價 {limit} 買 {sizer.fmt_shares(remain)} 等回檔"}
        return {**base, "status": "不追價", "remaining": remain,
                "advice": f"現價 {price} 已高於限價 {gap:.1f}%：不追高，掛限價 {limit}；下一輪會依新價格重算"}
    if held == 0:
        return {**base, "status": "已成交", "advice": "已無持股，這筆已完成"}
    if price is None:
        return {**base, "status": "無報價", "advice": f"暫無即時報價，掛限價 {limit} 賣 {sizer.fmt_shares(min(held, o['shares']))}"}
    if q.get("at_limit_down"):
        return {**base, "status": "跌停賣不掉", "advice": f"跌停鎖住，掛跌停價 {q.get('limit_down')} 排隊，打開時優先成交"}
    return {**base, "status": "可以下單", "advice": f"賣出 {sizer.fmt_shares(min(held, o['shares']))}，限價 {limit}（現價 {price}）"}


def _compare(plan_orders: list[dict], morning: list[dict], skipped: list[dict]) -> list[dict]:
    """與早上盤前報告的訂單比較。"""
    out = []
    m = {(o["code"], o["action"]): o for o in morning}
    p = {(o["code"], o["action"]): o for o in plan_orders}
    skip_reason = {s["code"]: s["reason"] for s in skipped}
    for k, o in p.items():
        if k not in m:
            out.append({"code": o["code"], "name": o["name"], "change": "新增", "detail": f"{'買進' if o['action'] == 'BUY' else '賣出'} {sizer.fmt_shares(o['shares'])}"})
        elif abs(o["shares"] - m[k]["shares"]) > max(1, m[k]["shares"] * 0.1):
            out.append({"code": o["code"], "name": o["name"], "change": "數量調整",
                        "detail": f"{sizer.fmt_shares(m[k]['shares'])} → {sizer.fmt_shares(o['shares'])}（依現價重新配置）"})
    for k, o in m.items():
        if k not in p:
            out.append({"code": o["code"], "name": o["name"], "change": "取消",
                        "detail": skip_reason.get(o["code"], "依現價重算後分數或排名下降，不再列入")})
    return out


def run(report: dict | None) -> dict:
    """report：最新每日報告（用來比較早盤計畫）。"""
    from src.agents import on_demand

    now = now_tw()
    lp = on_demand.live_plan(realtime=True)
    pf = pfm.build_portfolio()
    rt = lp["rt"]
    morning = [o for o in (report or {}).get("orders", []) if (report or {}).get("target_date") == now.date().isoformat()]
    held = {c: p.shares for c, p in pf.positions.items()}
    order_rows = []
    for o in lp["orders"]:
        o = {**o, "existing_before": held.get(o["code"], 0) if o["action"] == "BUY" else 0}
        order_rows.append(_order_status(o, rt, held.get(o["code"], 0), market_open(current_slot(now))))
    changes = _compare(lp["orders"], morning, lp["skipped"]) if morning else []

    nav, ratio = lp["nav"], lp["holding_ratio"]
    min_ratio = rules()["requirements"]["min_holding_ratio"]
    build_end = dt.date.fromisoformat(str(rules()["competition"]["build_period_end"]))
    st = params.strategy()
    ratio_note = None
    if ratio < min_ratio:
        need = (min_ratio - ratio) * nav
        ratio_note = (f"目前實際持股率 {ratio * 100:.1f}%，低於 {min_ratio * 100:.0f}%，還差約 {need:,.0f} 元；"
                      + ("收盤後會扣報酬率 0.2%，請在 13:30 前讓買單成交（可改用較積極的限價）" if now.date() > build_end
                         else "目前仍在建倉期（10/16 前不罰），但請盡快建立部位"))
    elif ratio < st["target_holding_ratio"] - 0.05:
        ratio_note = f"目前實際持股率 {ratio * 100:.1f}%，高於 70% 下限，但低於目標 {st['target_holding_ratio'] * 100:.0f}%"

    building = not pf.positions
    actions = []
    for r in order_rows:
        if r["status"] in ("可以下單", "可預掛單"):
            tag = "預掛" if r["status"] == "可預掛單" else ("建倉" if building else ("買進" if r["action"] == "BUY" else "賣出"))
            actions.append(f"【{tag}】{r['code']} {r['name']}：{r['advice']}")
    if ratio_note and ratio < min_ratio and not building:
        actions.append(f"【持股率】{ratio_note}")
    from src.agents import alerts

    rule_notes = alerts.rule_alerts(pf, lp["orders"], now.date())
    actions += [f"【規則】{a['title']}：{a['action']}" for a in rule_notes if a["level"] == "嚴重"]
    waits = [f"{r['code']} {r['name']}：{r['advice']}" for r in order_rows if r["status"] in ("接近限價", "不追價", "漲停買不到", "跌停賣不掉", "無報價")]
    if lp["funding"]["transfer_from_futures"] > 0 and building:
        actions.insert(0, f"【資金】下單前先把期貨戶 {lp['funding']['transfer_from_futures']:,} 元轉到證券戶，持股率才拉得上去")
    regime = _regime(now)
    if regime and regime["level"] in ("黃燈", "紅燈"):
        actions.insert(0, f"【大盤{regime['level']}】{regime['headline']}；" + regime["actions"][0])
    return {
        "regime": regime,
        "time": now.strftime("%Y-%m-%d %H:%M"), "slot": current_slot(now),
        "is_last_check": current_slot(now).endswith("13:00"), "market_open": market_open(current_slot(now)),
        "building": building, "taiex": _taiex_rt(), "nav": nav, "holding_ratio": ratio,
        "return_pct": round((nav / rules()["capital"]["initial"] - 1) * 100, 3),
        "allocation": {k: lp["allocation"].get(k) for k in ("offense", "defense", "etf", "stock_split", "etf_reasons")},
        "derivatives": slim_deriv(lp.get("derivatives") or {}), "recovery": lp.get("recovery"),
        "funding": lp.get("funding"),
        "orders": order_rows, "holding_items": lp["items"], "holding_alerts": lp["hold_alerts"],
        "skipped": lp["skipped"], "changes": changes, "actions": actions, "waits": waits, "ratio_note": ratio_note,
        "rule_notes": rule_notes,
        "summary": ("建倉中：" if building else "") + (f"{len(actions)} 項可以動作" if actions else "目前不需要動作，維持原掛單"),
    }


def _regime(now) -> dict | None:
    """大盤燈號＋盤中預判（即時加權指數是否跌破轉弱線／站回）。失敗不影響盤中建議。"""
    try:
        import pandas as pd

        from src.agents import market_data, regime

        tx = market_data.taiex(3)
        tx = tx[tx.index < pd.Timestamp(now.date())]
        live = (_taiex_rt() or {}).get("price")
        r = regime.assess(tx, market_data.us_history(), live_price=live)
        return {k: r[k] for k in ("state", "light", "level", "headline", "warnings", "actions", "lines", "offense")}
    except Exception as e:  # noqa: BLE001
        log.warning("大盤燈號計算失敗：%s", e)
        return None


def slim_deriv(d: dict) -> dict:
    """期貨建議精簡版（前端與 Email 用）：不含回測明細以外的大型欄位。"""
    sig = d.get("signal") or {}
    return {"summary": d.get("summary"), "mode": d.get("mode"), "orders": d.get("orders", []), "hedge": d.get("hedge"),
            "reasons": d.get("reasons", []), "reserve": d.get("reserve"), "margin_needed": d.get("margin_needed"),
            "conditions": d.get("conditions", []), "reference": d.get("reference"),
            "quote": d.get("quote"), "contract_month": d.get("contract_month"),
            "signal": {k: sig.get(k) for k in ("date", "p_up", "horizon_days", "long_enabled", "short_enabled",
                                               "gate_reasons", "drivers", "backtest")} if sig else None}


def save(out: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = out["time"].replace("-", "").replace(" ", "_").replace(":", "")
    (OUT_DIR / f"{stamp}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def email_body(out: dict) -> str:
    tx = out.get("taiex") or {}
    title = "建倉建議" if out["building"] else "盤中建議"
    lines = [f"{title}（{slot_label(out['slot'])}{'' if not market_open(out['slot']) else ' 時段'}，每 40 分鐘更新）" + ("・收盤前最後檢查" if out["is_last_check"] else ""),
             f"加權指數 {tx.get('price', '—')}（{tx.get('chg_pct', 0):+.2f}%）｜總資產 {out['nav']:,} 元｜實際持股率 {out['holding_ratio'] * 100:.1f}%"
             f"｜攻守 {out['allocation']['offense'] * 100:.0f}/{out['allocation']['defense'] * 100:.0f}",
             ""]
    rg = out.get("regime")
    if rg:
        lines += [f"大盤燈號：{rg['headline']}"] + [f"- {w}" for w in rg["warnings"]] + [
            f"- 轉紅燈（撤資）線：收盤跌破 {rg['lines']['weak_enter']:,}；綠燈解除線：{rg['lines']['strong_exit']:,}", ""]
    lines += ["現在可以做：" if out["actions"] else "現在可以做：無，維持原掛單"]
    lines += [f"- {a}" for a in out["actions"]]
    if out["waits"]:
        lines += ["", "掛單等待："] + [f"- {w}" for w in out["waits"]]
    if out["changes"]:
        lines += ["", "和早上計畫相比："] + [f"- {c['change']}｜{c['code']} {c['name']}：{c['detail']}" for c in out["changes"]]
    dv = out.get("derivatives") or {}
    if dv:
        lines += ["", f"期貨與選擇權：{dv.get('summary')}"]
        for o in dv.get("orders", []):
            lines.append(f"- {'買進' if o['action'] == 'BUY' else '賣出'} {o['contract']} {o['qty']} 口，限價 {o['limit_price']:,}、"
                         f"停損 {o['stop']:,}、保證金 {o['margin']:,} 元、最大損失 {o['max_loss']:,} 元")
        if dv.get("hedge"):
            h = dv["hedge"]
            lines.append(f"- 避險：買進 {h['name']} {h['qty']} 口，權利金約 {h['premium']:,} 元（最大損失）")
        if not dv.get("orders") and not dv.get("hedge"):
            lines += [f"- {r}" for r in (dv.get("reasons") or [])[-2:]]
    if out["skipped"]:
        lines += ["", "今天不追："] + [f"- {s['code']} {s['name']}：{s['reason']}" for s in out["skipped"]]
    holds = [h for h in out["holding_items"] if h["verdict"] == "續抱"]
    if holds:
        lines += ["", "續抱：" + "、".join(f"{h['code']} {h['name']}" for h in holds)]
    if out["ratio_note"] and not any(a.startswith("【持股率】") for a in out["actions"]):
        lines += ["", out["ratio_note"]]
    notes = [a for a in out.get("rule_notes", []) if a["level"] != "嚴重"]
    if notes:
        lines += ["", "競賽規則提醒："] + [f"- {a['title']}：{a['action']}" for a in notes]
    return "\n".join(lines)
