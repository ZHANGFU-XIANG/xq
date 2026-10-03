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
    add("投資組合至少 5 個標的", len(held_after) >= req["min_symbols"],
        f"執行後持有 {len(held_after)} 檔；累計交易過 {len(symbols)} 檔")

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
