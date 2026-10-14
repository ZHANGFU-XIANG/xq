"""CalcAgent：交易相關計算的唯一來源（與競賽 App 相同的進位規則），AI 助理用 calculate 工具呼叫，不讓 LLM 心算。

- cost：一筆成交的價金、手續費、證交稅、淨收付（買進價金捨去、賣出交割無條件進位）
- pnl：買進到賣出的淨損益（含雙邊手續費與證交稅）與報酬率
- shares_for_budget：預算買得到幾股（整張＋零股）與總成本
- breakeven：扣完成本要漲到多少才打平
"""
from __future__ import annotations

import math

from src.state import portfolio as pfm


def cost(code: str, side: str, shares: int, price: float) -> dict:
    side = side.upper()
    amount = pfm.trade_amount(shares, price, side)
    fee, tax = pfm.fee_and_tax(code, side, shares, price)
    settle = math.ceil(shares * price - 1e-6) if side == "SELL" else amount
    net = settle - fee - tax if side == "SELL" else -(amount + fee)
    return {"code": code, "side": side, "shares": shares, "price": price, "amount": amount, "fee": fee, "tax": tax,
            "net_cash": net, "note": "手續費 0.1425% 無條件進位、最低 20 元；證交稅股票 0.3%、ETF 0.1%（與競賽 App 相同）"}


def pnl(code: str, shares: int, buy_price: float, sell_price: float) -> dict:
    b, s = cost(code, "BUY", shares, buy_price), cost(code, "SELL", shares, sell_price)
    profit = s["net_cash"] + b["net_cash"]
    return {"code": code, "shares": shares, "buy_cost": -b["net_cash"], "sell_receive": s["net_cash"], "profit": profit,
            "return_pct": round(profit / -b["net_cash"] * 100, 3) if b["net_cash"] else None,
            "costs": b["fee"] + s["fee"] + s["tax"]}


def shares_for_budget(code: str, budget: float, price: float, odd_lot: bool = True) -> dict:
    per = price * (1 + 0.001425)
    total = int(budget // per)
    lots, odd = divmod(total, 1000)
    if not odd_lot:
        odd, total = 0, lots * 1000
    c = cost(code, "BUY", total, price) if total else {"net_cash": 0, "fee": 0}
    return {"code": code, "budget": budget, "price": price, "shares": total, "lots": lots, "odd": odd,
            "total_cost": -c["net_cash"], "fee": c["fee"], "left": round(budget + c["net_cash"])}


def breakeven(code: str, shares: int, buy_price: float) -> dict:
    lo, hi = buy_price, buy_price * 1.05
    for _ in range(50):
        mid = (lo + hi) / 2
        if pnl(code, shares, buy_price, mid)["profit"] >= 0:
            hi = mid
        else:
            lo = mid
    return {"code": code, "shares": shares, "buy_price": buy_price, "breakeven_price": round(hi, 2),
            "breakeven_pct": round((hi / buy_price - 1) * 100, 3)}


OPS = {"cost": cost, "pnl": pnl, "shares_for_budget": shares_for_budget, "breakeven": breakeven}


def calculate(op: str, **kw) -> dict:
    if op not in OPS:
        return {"error": f"op 只支援 {'、'.join(OPS)}"}
    return OPS[op](**kw)
