"""ComplianceAgent：依競賽規則檢查目前與「執行建議後」的投資組合。"""
from __future__ import annotations

import datetime as dt
import re

from src.config import is_trading_day, rules
from src.state import params
from src.state.portfolio import Portfolio


def is_warrant(code: str) -> bool:
    """權證為 6 碼數字且非 00 開頭（00 開頭為 ETF）。"""
    return bool(re.fullmatch(r"\d{6}", code)) and not code.startswith("00")


def remaining_trading_days(today: dt.date) -> int:
    end = dt.date.fromisoformat(str(rules()["competition"]["end"]))
    d, n = today, 0
    while d <= end:
        n += is_trading_day(d)
        d += dt.timedelta(days=1)
    return n


def check(pf: Portfolio, prices: dict, orders: list[dict], today: dt.date, nav: float) -> list[dict]:
    r = rules()
    req = r["requirements"]
    st = params.strategy()
    build_end = dt.date.fromisoformat(str(r["competition"]["build_period_end"]))
    out = []

    def add(item, ok, detail, warn=False):
        out.append({"item": item, "status": "通過" if ok else ("注意" if warn else "未通過"), "detail": detail})

    # 禁止權證
    bad = [o["code"] for o in orders if is_warrant(o["code"])]
    add("禁止交易權證", not bad, "無權證標的" if not bad else f"移除權證：{', '.join(bad)}")

    # 持股率（目前 / 執行後）
    cur_ratio = pf.holding_ratio(prices)
    mv_after = pf.market_value(prices)
    for o in orders:
        price = o.get("limit_price") or prices.get(o["code"], 0)
        mv_after += o["shares"] * price * (1 if o["action"] == "BUY" else -1)
    ratio_after = mv_after / nav if nav else 0
    in_build = today <= build_end
    min_ratio = req["min_holding_ratio"]
    add("目前持股率 ≥ 70%", cur_ratio >= min_ratio,
        f"目前 {cur_ratio * 100:.1f}%" + ("（建倉期至 10/16，不罰）" if in_build else "（每日收盤檢查，未達扣報酬率 0.2%）"),
        warn=in_build)
    add("執行建議後持股率 ≥ 70%", ratio_after >= min_ratio,
        f"預估 {ratio_after * 100:.1f}%（目標 {st['target_holding_ratio'] * 100:.0f}%）；注意只算實際成交，掛單未成交不算")

    # 標的數
    symbols = set(pf.traded_symbols) | {o["code"] for o in orders}
    held_after = {c for c, p in pf.positions.items() if p.shares > 0}
    for o in orders:
        if o["action"] == "BUY":
            held_after.add(o["code"])
        elif o.get("holding_after", 1) == 0:
            held_after.discard(o["code"])
    sym_start = dt.date.fromisoformat(str(req.get("symbols_rule_start", build_end)))
    before = today < sym_start
    add("交易過的標的 ≥ 5（每日收盤追蹤）", len(symbols) >= req["min_symbols"],
        f"已成交 {len(pf.traded_symbols)} 檔＋本次建議 = {len(symbols)} 檔；執行後持有 {len(held_after)} 檔"
        + (f"（第三週 {sym_start.month}/{sym_start.day} 起才檢查）" if before else "（未達每次扣報酬率 0.2%）"), warn=before)

    # 最後一日規定（Q&A）：收盤後仍須持有 ≥5 檔、現金 < 30%
    fd = req.get("final_day") or {}
    if fd and remaining_trading_days(today) <= 5:
        cash_after = pf.cash - sum(o["shares"] * o["limit_price"] * (1 if o["action"] == "BUY" else -1) for o in orders
                                   if o.get("asset", "STOCK") in ("STOCK", "ETF"))
        cash_ratio = max(0.0, cash_after + pf.futures_cash) / nav if nav else 1
        add(f"最後一日收盤仍持有 ≥ {fd['min_held_symbols']} 檔", len(held_after) >= fd["min_held_symbols"],
            f"執行後持有 {len(held_after)} 檔；最後一天收盤後不可清空部位")
        add(f"最後一日現金 < 總資產 {fd['max_cash_ratio'] * 100:.0f}%", cash_ratio < fd["max_cash_ratio"],
            f"預估現金（含期貨戶）佔總資產 {cash_ratio * 100:.1f}%")

    # 交易筆數進度
    done = pf.trade_count + len(orders)
    remain = remaining_trading_days(today)
    ok = done >= req["min_trades"]
    add("總交易筆數 ≥ 10", ok, f"累計 {pf.trade_count} 筆＋本次建議 {len(orders)} 筆 = {done} 筆；剩餘 {remain} 個交易日",
        warn=not ok and remain > 5)

    # 資金與融資
    loan = pf.margin_loan() + sum(o["amount"] * r["credit"]["margin_ratio"] for o in orders if o.get("margin"))
    cap = st["max_margin_loan_ratio"] * nav
    add("融資借款在自訂上限內", loan <= cap + 1, f"融資借款 {loan:,.0f} 元 / 上限 {cap:,.0f} 元（總資產 {st['max_margin_loan_ratio'] * 100:.0f}%）")

    # 報酬為正才排名
    ret = nav / r["capital"]["initial"] - 1
    add("累積報酬為正（才列入排名）", ret > 0, f"目前累積報酬 {ret * 100:+.2f}%", warn=True)
    return out


# ---------------------------------------------------------------- 收盤前倒數（使用者 2026-10-15：避免每次扣 0.2% 或最後一天被取消排名）
def close_check(now: dt.datetime | None = None, buffer: float = 0.02) -> dict:
    """用即時價估算「如果現在收盤」的持股率、交易標的數與最後一日規定，不足時算出 13:30 前要補多少。
    buffer：持股率目標多抓 2%（收盤價波動、手續費、零股未成交）。"""
    from src import pipeline
    from src.agents import alerts
    from src.config import now_tw
    from src.state import portfolio as pfm

    now = now or now_tw()
    today = now.date()
    r = rules()
    req = r["requirements"]
    pf = pfm.build_portfolio()
    rt = alerts.realtime(list(pf.positions)) if pf.positions else {}
    prices = {c: (rt.get(c) or {}).get("price") or p.avg_cost for c, p in pf.positions.items()}
    marked = pipeline.with_marks(pf, prices)
    ratio = pf.holding_ratio(marked)
    mv = pf.market_value(marked)
    denom = mv / ratio if ratio else pf.nav(marked)
    build_end = dt.date.fromisoformat(str(r["competition"]["build_period_end"]))
    sym_start = dt.date.fromisoformat(str(req.get("symbols_rule_start", build_end)))
    min_ratio = req["min_holding_ratio"]
    items = []
    ratio_active = today > build_end
    need = max(0.0, (min_ratio + buffer) * denom - mv)  # 用現金買股票：分母幾乎不變
    items.append({"key": "ratio", "ok": ratio >= min_ratio + buffer or not ratio_active, "active": ratio_active,
                  "value": round(ratio, 4), "need_amount": round(need),
                  "text": (f"估計收盤持股率 {ratio * 100:.1f}%" + ("" if ratio >= min_ratio + buffer else
                           f"，低於安全線 {(min_ratio + buffer) * 100:.0f}%：13:30 前再買約 {need:,.0f} 元股票")
                           + ("" if ratio_active else "（建倉期不罰）"))})
    n_sym = len(pf.traded_symbols)
    sym_active = today >= sym_start
    items.append({"key": "symbols", "ok": n_sym >= req["min_symbols"] or not sym_active, "active": sym_active,
                  "value": n_sym, "text": f"交易過的標的 {n_sym} 檔（需 ≥ {req['min_symbols']}）"
                  + ("" if sym_active else f"，{sym_start:%m/%d} 起每天檢查")})
    left = remaining_trading_days(today)
    fd = req.get("final_day") or {}
    if fd and left <= 5:
        held = len([p for p in pf.positions.values() if p.shares > 0])
        cash_ratio = max(0.0, pf.cash + pf.futures_cash) / pf.nav(marked) if pf.nav(marked) else 1
        items.append({"key": "final_held", "ok": held >= fd["min_held_symbols"], "active": True, "value": held,
                      "text": f"最後一天（剩 {left} 個交易日）收盤須持有 ≥ {fd['min_held_symbols']} 檔：目前 {held} 檔"})
        items.append({"key": "final_cash", "ok": cash_ratio < fd["max_cash_ratio"], "active": True, "value": round(cash_ratio, 4),
                      "text": f"最後一天現金須 < {fd['max_cash_ratio'] * 100:.0f}%：目前 {cash_ratio * 100:.1f}%"})
    bad = [i for i in items if not i["ok"]]
    return {"time": now.strftime("%H:%M"), "items": items, "ok": not bad, "days_left": left,
            "alerts": [{"key": f"rule-{i['key']}", "level": "嚴重", "title": "收盤前規則檢查未通過", "body": i["text"]} for i in bad]}
