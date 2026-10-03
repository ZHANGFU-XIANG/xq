"""投資組合狀態：以 data/trades.csv（實際成交紀錄）為唯一真實來源，重播計算持倉與現金。

trades.csv 欄位：date,code,side,shares,price,margin,note
- side：BUY / SELL
- margin：1 表示融資買進（自備 40%），0 為現股
系統輔助資訊（停損價、停利階段、進場後最高價）存於 data/position_meta.json。
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field

from src.config import DATA_DIR, rules, stock_meta

TRADES_CSV = DATA_DIR / "trades.csv"
META_JSON = DATA_DIR / "position_meta.json"
HISTORY_JSON = DATA_DIR / "nav_history.json"
TRADE_FIELDS = ["date", "code", "side", "shares", "price", "margin", "note"]


def is_etf(code: str) -> bool:
    return code.startswith("00") or bool(stock_meta(code).get("is_etf"))


def trade_cost(code: str, side: str, shares: int, price: float) -> float:
    c = rules()["costs"]
    amount = shares * price
    fee = max(c["min_commission"], round(amount * c["commission"])) if shares else 0
    tax = round(amount * (c["etf_tax"] if is_etf(code) else c["stock_tax"])) if side == "SELL" else 0
    return float(fee + tax)


@dataclass
class Position:
    code: str
    shares: int = 0
    cost_basis: float = 0.0        # 含手續費的總成本
    margin_loan: float = 0.0       # 融資借款

    @property
    def avg_cost(self) -> float:
        return self.cost_basis / self.shares if self.shares else 0.0


@dataclass
class Portfolio:
    cash: float
    futures_cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    trade_count: int = 0
    traded_symbols: set[str] = field(default_factory=set)
    realized_pnl: float = 0.0
    trades: list[dict] = field(default_factory=list)

    def market_value(self, prices: dict[str, float]) -> float:
        return sum(p.shares * prices.get(c, p.avg_cost) for c, p in self.positions.items())

    def margin_loan(self) -> float:
        return sum(p.margin_loan for p in self.positions.values())

    def nav(self, prices: dict[str, float]) -> float:
        """總資產（權益）= 現金 + 期貨帳戶 + 持股市值 − 融資借款。"""
        return self.cash + self.futures_cash + self.market_value(prices) - self.margin_loan()

    def holding_ratio(self, prices: dict[str, float]) -> float:
        """持股率 = 持有部位市值 / 總資產（規則：須 ≥ 70%）。"""
        nav = self.nav(prices)
        return self.market_value(prices) / nav if nav > 0 else 0.0


def load_trades() -> list[dict]:
    if not TRADES_CSV.exists():
        return []
    with TRADES_CSV.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("code")]


def append_trades(rows: list[dict]) -> None:
    new = not TRADES_CSV.exists()
    with TRADES_CSV.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in TRADE_FIELDS})


def build_portfolio(trades: list[dict] | None = None) -> Portfolio:
    cap = rules()["capital"]
    credit = rules()["credit"]
    pf = Portfolio(cash=float(cap["securities"]), futures_cash=float(cap["futures"]))
    for t in trades if trades is not None else load_trades():
        code, side = t["code"].strip(), t["side"].strip().upper()
        shares, price = int(float(t["shares"])), float(t["price"])
        margin = str(t.get("margin", "0")).strip() in ("1", "true", "True", "Y")
        cost = trade_cost(code, side, shares, price)
        pos = pf.positions.setdefault(code, Position(code))
        if side == "BUY":
            loan = round(shares * price * credit["margin_ratio"]) if margin else 0.0
            pf.cash -= shares * price + cost - loan
            pos.shares += shares
            pos.cost_basis += shares * price + cost
            pos.margin_loan += loan
        elif side == "SELL":
            if shares > pos.shares:
                raise ValueError(f"{t['date']} {code} 賣出 {shares} 股超過持有 {pos.shares} 股")
            frac = shares / pos.shares
            repay = pos.margin_loan * frac
            basis = pos.cost_basis * frac
            proceeds = shares * price - cost
            pf.cash += proceeds - repay
            pf.realized_pnl += proceeds - basis
            pos.shares -= shares
            pos.cost_basis -= basis
            pos.margin_loan -= repay
        else:
            raise ValueError(f"未知交易方向 {side}")
        pf.trade_count += 1
        pf.traded_symbols.add(code)
        pf.trades.append({**t, "cost": cost})
        if pos.shares == 0:
            del pf.positions[code]
    return pf


def load_meta() -> dict:
    return json.loads(META_JSON.read_text(encoding="utf-8")) if META_JSON.exists() else {}


def save_meta(meta: dict) -> None:
    META_JSON.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def load_history() -> list[dict]:
    return json.loads(HISTORY_JSON.read_text(encoding="utf-8")) if HISTORY_JSON.exists() else []


def record_history(date: str, nav: float, holding_ratio: float) -> list[dict]:
    hist = [h for h in load_history() if h["date"] != date]
    initial = rules()["capital"]["initial"]
    hist.append({"date": date, "nav": round(nav), "holding_ratio": round(holding_ratio, 4),
                 "return_pct": round((nav / initial - 1) * 100, 3)})
    hist.sort(key=lambda h: h["date"])
    HISTORY_JSON.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
    return hist
