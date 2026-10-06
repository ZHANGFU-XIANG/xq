"""交易紀錄統計與檢討：由 data/trades.csv 重播計算，數字全由程式算（不經 LLM）。

- 已實現交易：每次賣出一列，依先進先出對應到買進批次，算出加權買進價、持有交易日（依股數加權）、損益（含手續費與證交稅）
- 勝率（以每次賣出計）、平均獲利／虧損、獲利因子、平均持有天數
- 每檔統計：買賣次數、買進／賣出金額、已實現、未實現、手續費、證交稅
- 每日統計：買進／賣出金額、筆數、當日已實現損益
- 競賽門檻：交易筆數、交易過的標的數
期貨／選擇權與資金調撥、手續費校正列不列入股票統計，只計入筆數與總額。
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from src.config import is_trading_day, rules, stock_meta
from src.state import portfolio as pfm


def _tdays(d0: str, d1: str) -> int | None:
    """d0 買進到 d1 賣出經過幾個交易日（同一天為 0）。"""
    try:
        a, b = dt.date.fromisoformat(d0), dt.date.fromisoformat(d1)
    except ValueError:
        return None
    n = 0
    while a < b:
        a += dt.timedelta(days=1)
        n += is_trading_day(a)
    return n


def fee_tax(code: str, side: str, shares: int, price: float) -> tuple[float, float]:
    """拆開手續費與證交稅（合計等於 portfolio.trade_cost）。"""
    c = rules()["costs"]
    amount = shares * price
    fee = max(c["min_commission"], round(amount * c["commission"])) if shares else 0
    tax = round(amount * (c["etf_tax"] if pfm.is_etf(code) else c["stock_tax"])) if side == "SELL" else 0
    return float(fee), float(tax)


def _name(code: str) -> str:
    try:
        return stock_meta(code).get("name") or ""
    except Exception:  # noqa: BLE001
        return ""


def review(trades: list[dict], prices: dict[str, float]) -> dict:
    """trades：trades.csv 原始列；prices：目前價格（算未實現損益用）。"""
    lots: dict[str, list[dict]] = defaultdict(list)  # 每檔未賣出的買進批次 {date, shares, price, unit_cost}
    per: dict[str, dict] = {}
    days: dict[str, dict] = {}
    closed: list[dict] = []
    other = {"deriv": 0, "transfer": 0, "fee_adj": 0.0}
    for t in trades:
        code, side = str(t["code"]).strip(), str(t["side"]).strip().upper()
        if code.upper() == "FEE":
            other["fee_adj"] += float(t["price"])
            continue
        if code.upper() == "TRANSFER":
            other["transfer"] += 1
            continue
        if pfm.asset_of(t) in ("FUT", "OPT"):
            other["deriv"] += 1
            dd = days.setdefault(t.get("date", ""), {"date": t.get("date", ""), "count": 0, "buy_amount": 0.0, "sell_amount": 0.0,
                                                    "realized": 0.0, "fee_tax": 0.0})
            dd["count"] += 1  # 期權只計筆數；金額與損益看「期貨／選擇權已實現」
            continue
        shares, price, date = int(float(t["shares"])), float(t["price"]), t.get("date", "")
        fee, tax = fee_tax(code, side, shares, price)
        s = per.setdefault(code, {"code": code, "name": _name(code), "buys": 0, "sells": 0, "buy_shares": 0, "sell_shares": 0,
                                  "buy_amount": 0.0, "sell_amount": 0.0, "fee": 0.0, "tax": 0.0, "realized": 0.0,
                                  "first_date": date, "last_date": date})
        d = days.setdefault(date, {"date": date, "count": 0, "buy_amount": 0.0, "sell_amount": 0.0, "realized": 0.0, "fee_tax": 0.0})
        s["fee"] += fee
        s["tax"] += tax
        s["last_date"] = max(s["last_date"], date)
        d["count"] += 1
        d["fee_tax"] += fee + tax
        if side == "BUY":
            s["buys"] += 1
            s["buy_shares"] += shares
            s["buy_amount"] += shares * price
            d["buy_amount"] += shares * price
            lots[code].append({"date": date, "shares": shares, "price": price, "unit_cost": (shares * price + fee) / shares})
            continue
        s["sells"] += 1
        s["sell_shares"] += shares
        s["sell_amount"] += shares * price
        d["sell_amount"] += shares * price
        net_unit = (shares * price - fee - tax) / shares  # 每股實收（扣手續費與證交稅）
        left, pnl, cost, gross, day_sh = shares, 0.0, 0.0, 0.0, 0
        first = lots[code][0]["date"] if lots[code] else date
        while left > 0 and lots[code]:  # 先進先出：一次賣出可能跨好幾個買進批次
            lot = lots[code][0]
            take = min(left, lot["shares"])
            pnl += take * (net_unit - lot["unit_cost"])
            cost += take * lot["unit_cost"]
            gross += take * lot["price"]
            day_sh += take * (_tdays(lot["date"], date) or 0)
            lot["shares"] -= take
            left -= take
            if lot["shares"] == 0:
                lots[code].pop(0)
        closed.append({"code": code, "name": s["name"], "buy_date": first, "sell_date": date, "shares": shares,
                       "buy_price": round(gross / shares, 3), "sell_price": price, "pnl": round(pnl),
                       "ret_pct": round((shares * net_unit / cost - 1) * 100, 2) if cost else None,
                       "hold_days": round(day_sh / shares, 1), "fee_tax": round(fee + tax), "note": t.get("note") or ""})
        s["realized"] += pnl
        d["realized"] += pnl

    # 每檔：目前持有與未實現（同競賽 App：市值 − 成交價金，不含手續費）
    for code, s in per.items():
        held = sum(lt["shares"] for lt in lots[code])
        gross = sum(lt["shares"] * lt["price"] for lt in lots[code])
        px = prices.get(code)
        s["held_shares"] = held
        s["price"] = px
        s["unrealized"] = round(held * px - gross) if held and px else 0
        s["total_pnl"] = round(s["realized"] + s["unrealized"])
        for k in ("buy_amount", "sell_amount", "fee", "tax", "realized"):
            s[k] = round(s[k])
        s["avg_buy"] = round(s["buy_amount"] / s["buy_shares"], 3) if s["buy_shares"] else None
        s["avg_sell"] = round(s["sell_amount"] / s["sell_shares"], 3) if s["sell_shares"] else None

    wins = [c["pnl"] for c in closed if c["pnl"] > 0]
    losses = [c["pnl"] for c in closed if c["pnl"] <= 0]
    sold = sum(c["shares"] for c in closed)
    fee_total = sum(s["fee"] for s in per.values())
    tax_total = sum(s["tax"] for s in per.values())
    realized = sum(s["realized"] for s in per.values())
    unreal = sum(s["unrealized"] for s in per.values())
    trade_count = sum(s["buys"] + s["sells"] for s in per.values()) + other["deriv"]
    req = rules().get("requirements", {})
    min_trades, min_symbols = req.get("min_trades", 10), req.get("min_symbols", 5)
    summary = {
        "trade_count": trade_count, "stock_trades": trade_count - other["deriv"], "deriv_trades": other["deriv"],
        "symbols": len(per), "buy_count": sum(s["buys"] for s in per.values()), "sell_count": sum(s["sells"] for s in per.values()),
        "buy_amount": round(sum(s["buy_amount"] for s in per.values())), "sell_amount": round(sum(s["sell_amount"] for s in per.values())),
        "fee": round(fee_total), "tax": round(tax_total), "fee_adjust": round(other["fee_adj"]),
        "realized": round(realized), "unrealized": round(unreal), "total_pnl": round(realized + unreal),
        "closed_count": len(closed), "win_count": len(wins), "loss_count": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "avg_win": round(sum(wins) / len(wins)) if wins else None,
        "avg_loss": round(sum(losses) / len(losses)) if losses else None,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if wins and losses and sum(losses) < 0 else None,
        "avg_hold_days": round(sum(c["hold_days"] * c["shares"] for c in closed) / sold, 1) if sold else None,  # 依股數加權
        "best": max(per.values(), key=lambda s: s["total_pnl"])["code"] if per else None,
        "worst": min(per.values(), key=lambda s: s["total_pnl"])["code"] if per else None,
        "active_days": len(days),
        "checks": [
            {"item": f"交易筆數 ≥ {min_trades}（排名資格，買賣各算一筆）", "value": trade_count, "ok": trade_count >= min_trades},
            {"item": f"交易過的標的 ≥ {min_symbols} 檔（10/19 起每日檢查）", "value": len(per), "ok": len(per) >= min_symbols},
        ],
    }
    return {
        "summary": summary,
        "per_symbol": sorted(per.values(), key=lambda s: -s["total_pnl"]),
        "closed": closed[::-1],  # 最近的賣出在前
        "daily": [{**d, **{k: round(d[k]) for k in ("buy_amount", "sell_amount", "realized", "fee_tax")}}
                  for d in sorted(days.values(), key=lambda d: d["date"], reverse=True)],
    }
