"""投資組合狀態：以 data/trades.csv（實際成交紀錄）為唯一真實來源，重播計算持倉與現金。

trades.csv 欄位：date,code,side,shares,price,margin,note,asset,contract
- side：BUY / SELL；資金調撥用 code=TRANSFER、side=TO_FUTURES／TO_SECURITIES、price=金額
- 手續費校正：code=FEE、side=ADJ、price=金額（依競賽 App 淨收付金額補差額）
- margin：1 表示融資買進（自備 40%），0 為現股
- 每一列是一筆成交：同一檔分整股、零股或不同時間成交時各記一列，成本分批記錄（Position.lots），賣出先進先出
- asset：STOCK／ETF／FUT／OPT（舊資料沒有此欄時依代號自動判斷）
- 期貨：code=商品（TX／MTX／TMF），contract=TMF202610，shares=口數，price=成交點數
- 選擇權（只做買方）：code=TXO，contract=TXO202610P46000，price=權利金點數
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
TRADE_FIELDS = ["date", "code", "side", "shares", "price", "margin", "note", "asset", "contract"]
DERIV_ROOTS = {"TX", "MTX", "TMF", "TXO"}


def asset_of(t: dict) -> str:
    a = str(t.get("asset") or "").strip().upper()
    if a:
        return a
    code = str(t.get("code", "")).strip().upper()
    if code == "TXO":
        return "OPT"
    if code in DERIV_ROOTS:
        return "FUT"
    return "ETF" if is_etf(code) else "STOCK"


def deriv_spec(root: str) -> dict:
    return rules()["futures"]["contracts"][root]


def deriv_cost(root: str, qty: int, price: float) -> float:
    """期貨／選擇權成本：手續費（每口，假設值）＋交易稅（期貨 0.002%、選擇權依權利金）。"""
    fr = rules()["futures"]
    c = rules()["costs"]
    mult = deriv_spec(root)["multiplier"]
    tax_rate = c["options_tax"] if root == "TXO" else c["index_futures_tax"]
    return float(fr["commission_per_contract"].get(root, 30) * qty + round(price * mult * qty * tax_rate))


def parse_option(contract: str) -> dict:
    """TXO202610P46000 → {ym: 202610, cp: P, strike: 46000}"""
    body = contract[3:]
    i = max(body.find("P"), body.find("C"))
    return {"ym": body[:i], "cp": body[i], "strike": float(body[i + 1:])}


def is_etf(code: str) -> bool:
    return code.startswith("00") or bool(stock_meta(code).get("is_etf"))


def trade_cost(code: str, side: str, shares: int, price: float) -> float:
    c = rules()["costs"]
    amount = shares * price
    fee = max(c["min_commission"], round(amount * c["commission"])) if shares else 0
    tax = round(amount * (c["etf_tax"] if is_etf(code) else c["stock_tax"])) if side == "SELL" else 0
    return float(fee + tax)


@dataclass
class Lot:
    """一筆買進成交（整股、零股、不同時間各自一筆），各自的成本分開記錄；賣出時先進先出扣除。"""
    date: str
    shares: int
    price: float
    cost_basis: float              # 含手續費（部分賣出後按比例遞減）
    note: str = ""

    @property
    def unit_cost(self) -> float:
        return self.cost_basis / self.shares if self.shares else 0.0


@dataclass
class Position:
    code: str
    shares: int = 0
    cost_basis: float = 0.0        # 含手續費的總成本（＝各批成本合計）
    margin_loan: float = 0.0       # 融資借款
    lots: list[Lot] = field(default_factory=list)

    @property
    def avg_cost(self) -> float:
        return self.cost_basis / self.shares if self.shares else 0.0


@dataclass
class DerivPosition:
    """期貨（口數可正可負）或選擇權買方（口數只會是正）。"""
    contract: str
    root: str
    kind: str                      # FUT / OPT
    qty: int = 0
    avg_price: float = 0.0         # 期貨：點數；選擇權：權利金點數

    @property
    def multiplier(self) -> int:
        return deriv_spec(self.root)["multiplier"]

    def value(self, price: float | None) -> float:
        """期貨：未實現損益；選擇權：部位市值（權利金）。"""
        p = self.avg_price if price is None else price
        if self.kind == "FUT":
            return (p - self.avg_price) * self.multiplier * self.qty
        return p * self.multiplier * self.qty


@dataclass
class Portfolio:
    cash: float
    futures_cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    trade_count: int = 0
    traded_symbols: set[str] = field(default_factory=set)
    realized_pnl: float = 0.0
    trades: list[dict] = field(default_factory=list)
    derivs: dict[str, DerivPosition] = field(default_factory=dict)
    traded_derivs: set[str] = field(default_factory=set)

    def market_value(self, prices: dict[str, float]) -> float:
        """股票＋ETF 市值（持股率只算這部分；期貨是否計入依 rules.futures.count_in_holding）。"""
        return sum(p.shares * prices.get(c, p.avg_cost) for c, p in self.positions.items())

    def margin_loan(self) -> float:
        return sum(p.margin_loan for p in self.positions.values())

    def futures_equity(self, prices: dict[str, float]) -> float:
        """期貨戶權益數 = 期貨戶現金 + 期貨未實現損益 + 選擇權市值。"""
        return self.futures_cash + sum(d.value(prices.get(d.contract)) for d in self.derivs.values())

    def futures_notional(self, prices: dict[str, float]) -> float:
        return sum(abs(d.qty) * (prices.get(d.contract) or d.avg_price) * d.multiplier
                   for d in self.derivs.values() if d.kind == "FUT")

    def margin_required(self, margins: dict[str, dict], kind: str = "initial") -> float:
        """期貨所需保證金（選擇權買方不需保證金）。margins：{TX: {initial, maintenance}}。"""
        return sum(abs(d.qty) * margins.get(d.root, {}).get(kind, 0) for d in self.derivs.values() if d.kind == "FUT")

    def nav(self, prices: dict[str, float]) -> float:
        """總資產（權益）= 證券戶現金 + 期貨戶權益數 + 持股市值 − 融資借款。"""
        return self.cash + self.futures_equity(prices) + self.market_value(prices) - self.margin_loan()

    def holding_ratio(self, prices: dict[str, float]) -> float:
        """持股率 = 持有部位市值 / 總資產（規則：須 ≥ 70%）。預設只算股票＋ETF。"""
        nav = self.nav(prices)
        held = self.market_value(prices)
        if rules().get("futures", {}).get("count_in_holding"):
            held += self.futures_notional(prices)
        return held / nav if nav > 0 else 0.0


def load_trades() -> list[dict]:
    if not TRADES_CSV.exists():
        return []
    with TRADES_CSV.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("code")]


def append_trades(rows: list[dict]) -> None:
    new = not TRADES_CSV.exists()
    if not new:  # 舊檔沒有新欄位（asset、contract）→ 先補欄位再追加
        with TRADES_CSV.open(encoding="utf-8-sig", newline="") as f:
            header = next(csv.reader(f), [])
        if header != TRADE_FIELDS:
            old = load_trades()
            with TRADES_CSV.open("w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
                w.writeheader()
                for r in old:
                    w.writerow({k: r.get(k, "") or "" for k in TRADE_FIELDS})
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
        if code.upper() == "FEE":  # 手續費校正（App 依每一筆成交計算，與系統合併計算的差額；不算交易筆數）
            pf.cash -= float(t["price"])
            continue
        if code.upper() == "TRANSFER":  # 證券戶 ↔ 期貨戶資金調撥（不算交易筆數）
            amt = float(t["price"])
            sign = 1 if side == "TO_FUTURES" else -1
            pf.cash -= sign * amt
            pf.futures_cash += sign * amt
            continue
        if asset_of(t) in ("FUT", "OPT"):
            _apply_deriv(pf, t)
            continue
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
            pos.lots.append(Lot(t.get("date", ""), shares, price, shares * price + cost, t.get("note") or ""))
        elif side == "SELL":
            if shares > pos.shares:
                raise ValueError(f"{t['date']} {code} 賣出 {shares} 股超過持有 {pos.shares} 股")
            frac = shares / pos.shares
            repay = pos.margin_loan * frac
            basis = _consume_fifo(pos, shares)
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


def held_days(pos: Position, today) -> int:
    """最早一批買進到今天經過幾個交易日（買進當天為 0）。"""
    import datetime as dt

    from src.config import is_trading_day

    try:
        d = dt.date.fromisoformat(pos.lots[0].date)
    except (IndexError, ValueError):  # 沒有日期的舊資料視為已持有很久
        return 999
    n = 0
    while d < today:
        d += dt.timedelta(days=1)
        n += is_trading_day(d)
    return n


def _consume_fifo(pos: Position, shares: int) -> float:
    """賣出時從最早買進的那一批開始扣（先進先出），回傳賣出部分的成本。"""
    basis, left = 0.0, shares
    while left > 0 and pos.lots:
        lot = pos.lots[0]
        take = min(left, lot.shares)
        part = lot.cost_basis * take / lot.shares
        basis += part
        lot.cost_basis -= part
        lot.shares -= take
        left -= take
        if lot.shares == 0:
            pos.lots.pop(0)
    return basis


def _apply_deriv(pf: Portfolio, t: dict) -> None:
    root, side = t["code"].strip().upper(), t["side"].strip().upper()
    kind = asset_of(t)
    contract = (t.get("contract") or root).strip().upper()
    qty, price = int(float(t["shares"])), float(t["price"])
    cost = deriv_cost(root, qty, price)
    d = pf.derivs.setdefault(contract, DerivPosition(contract, root, kind))
    mult = d.multiplier
    signed = qty if side == "BUY" else -qty
    if kind == "OPT":
        if side == "SELL" and qty > d.qty:
            raise ValueError(f"{t['date']} {contract} 只做選擇權買方：賣出 {qty} 口超過持有 {d.qty} 口")
        if side == "BUY":
            pf.futures_cash -= price * mult * qty + cost
            d.avg_price = (d.avg_price * d.qty + price * qty) / (d.qty + qty)
            d.qty += qty
        else:
            proceeds = price * mult * qty - cost
            pf.futures_cash += proceeds
            pf.realized_pnl += proceeds - d.avg_price * mult * qty
            d.qty -= qty
    else:
        pf.futures_cash -= cost
        if d.qty == 0 or (d.qty > 0) == (signed > 0):  # 新倉或加碼
            d.avg_price = (d.avg_price * abs(d.qty) + price * qty) / (abs(d.qty) + qty)
            d.qty += signed
        else:  # 平倉（可能反手）
            close = min(qty, abs(d.qty))
            pnl = (price - d.avg_price) * mult * close * (1 if d.qty > 0 else -1)
            pf.futures_cash += pnl
            pf.realized_pnl += pnl
            d.qty += signed
            if d.qty and (d.qty > 0) == (signed > 0):  # 反手：剩餘口數以本次價格為新均價
                d.avg_price = price
    pf.trade_count += 1
    pf.traded_derivs.add(contract)
    pf.trades.append({**t, "cost": cost})
    if d.qty == 0:
        del pf.derivs[contract]


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
