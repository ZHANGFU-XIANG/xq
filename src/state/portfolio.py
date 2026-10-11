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
import hashlib
import math
import io
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
    """期貨／選擇權成本：每口手續費＋每口交易稅（無條件進位，與競賽 App 相同；10/06 大台 2 口 = 560 元驗證）。"""
    fr = rules()["futures"]
    c = rules()["costs"]
    mult = deriv_spec(root)["multiplier"]
    tax_rate = c["options_tax"] if root == "TXO" else c["index_futures_tax"]
    per_lot_tax = math.ceil(price * mult * tax_rate - 1e-9)
    return float((fr["commission_per_contract"].get(root, 30) + per_lot_tax) * qty)


def parse_option(contract: str) -> dict:
    """TXO202610P46000 → {ym: 202610, cp: P, strike: 46000}"""
    body = contract[3:]
    i = max(body.find("P"), body.find("C"))
    return {"ym": body[:i], "cp": body[i], "strike": float(body[i + 1:])}


def is_etf(code: str) -> bool:
    return code.startswith("00") or bool(stock_meta(code).get("is_etf"))


def trade_amount(shares: int, price: float, side: str = "BUY") -> int:
    """成交價金（與競賽 App 相同）：買進元以下無條件捨去（528 股 × 28.6 = 15,100.8 → 15,100）；
    賣出四捨五入（10/07 國票金零股 208 股 × 16.35 = 3,400.8 → 3,401，10/08 對帳已實現 −582）。"""
    if str(side).upper() == "SELL":
        return int(math.floor(shares * price + 0.5 + 1e-6))
    return int(math.floor(shares * price + 1e-6))


def fee_and_tax(code: str, side: str, shares: int, price: float) -> tuple[int, int]:
    """手續費：每筆成交 0.1425%、無條件進位、最低 20 元（App 驗證）。
    證交稅：賣出時 0.3%（ETF 0.1%），元以下捨去（法定計算方式；尚未有賣出紀錄可與 App 核對）。"""
    c = rules()["costs"]
    amount = trade_amount(shares, price, side)
    fee = max(c["min_commission"], math.ceil(amount * c["commission"] - 1e-9)) if shares else 0
    tax = math.floor(amount * (c["etf_tax"] if is_etf(code) else c["stock_tax"]) + 1e-9) if side == "SELL" else 0
    return int(fee), int(tax)


def trade_cost(code: str, side: str, shares: int, price: float) -> float:
    fee, tax = fee_and_tax(code, side, shares, price)
    return float(fee + tax)


@dataclass
class Lot:
    """一筆買進成交（整股、零股、不同時間各自一筆），各自的成本分開記錄；賣出時先進先出扣除。"""
    date: str
    shares: int
    price: float
    cost_basis: float              # 含手續費（部分賣出後按比例遞減）
    note: str = ""
    amount: float = 0.0            # 成交價金（不含手續費，元以下捨去；部分賣出後按比例遞減）
    odd: bool = False              # 零股成交（賣出時零股先扣零股、整股先扣整股，與競賽 App 相同）

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
        """持股率（競賽 App 算法，10/06 對帳 76.11%）：
        股票＋ETF 市值 ÷（市值 ＋ 持有現金 ＋ 期貨原始保證金 ＋ 期貨未實現）。
        持有現金已含期貨未實現，App 分母等於「淨值 ＋ 期貨未實現」（期貨未實現計入兩次），期貨不算持股。"""
        held = self.market_value(prices)
        if rules().get("futures", {}).get("count_in_holding"):
            held += self.futures_notional(prices)
        fut_unreal = sum(d.value(prices.get(c)) for c, d in self.derivs.items() if d.kind == "FUT")
        denom = self.nav(prices) + fut_unreal
        return held / denom if denom > 0 else 0.0


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


def trades_csv_text(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=TRADE_FIELDS, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") or "" for k in TRADE_FIELDS})
    return buf.getvalue()


def trades_version(rows: list[dict] | None = None) -> str:
    """交易紀錄內容的指紋：手動修改時用來確認編輯的是最新版本（避免覆蓋別處剛寫入的成交）。"""
    return hashlib.sha1(trades_csv_text(load_trades() if rows is None else rows).encode("utf-8")).hexdigest()[:12]


def write_trades(rows: list[dict]) -> None:
    """整份改寫 trades.csv（手動新增／修改／刪除用）。"""
    TRADES_CSV.write_text(trades_csv_text(rows), encoding="utf-8", newline="")


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
        amount = trade_amount(shares, price, side)
        pos = pf.positions.setdefault(code, Position(code))
        if side == "BUY":
            loan = round(amount * credit["margin_ratio"]) if margin else 0.0
            pf.cash -= amount + cost - loan
            pos.shares += shares
            pos.cost_basis += amount + cost
            pos.margin_loan += loan
            pos.lots.append(Lot(t.get("date", ""), shares, price, amount + cost, t.get("note") or "", amount, is_odd_fill(t)))
        elif side == "SELL":
            if shares > pos.shares:
                raise ValueError(f"{t['date']} {code} 賣出 {shares} 股超過持有 {pos.shares} 股")
            frac = shares / pos.shares
            repay = pos.margin_loan * frac
            basis = _consume_fifo(pos, shares, is_odd_fill(t))
            proceeds = amount - cost
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


def is_odd_fill(t: dict) -> bool:
    """這筆成交是不是零股：依備註的盤別（XQ 匯入會寫「整股／零股」）；沒有盤別時，不足 1000 股視為零股。"""
    note = t.get("note") or ""
    if "零股" in note:
        return True
    if "整股" in note:
        return False
    return int(float(t["shares"])) % 1000 != 0 and int(float(t["shares"])) < 1000


def take_lots(lots: list, shares: int, odd: bool = False) -> list[tuple]:
    """賣出時扣買進批次（會修改 lots），回傳 [(批次, 股數, 成交價金, 手續費)]。競賽 App 算法（10/08 對帳一致：已實現 −582、未實現 186,795）：
    - 零股賣出先扣零股買進的批次、整股賣出先扣整股批次，各自先進先出；同盤別不夠扣時再扣其他批次
    - 只賣掉某一批的一部分時，成交價金與手續費各自按股數比例計算，元以下無條件捨去
    lots 的元素要有 shares、amount、cost_basis（成交價金＋手續費）、odd。"""
    pieces, left = [], shares
    for lot in [x for x in lots if x.odd == odd] + [x for x in lots if x.odd != odd]:
        if left <= 0:
            break
        take = min(left, lot.shares)
        if take == lot.shares:
            amt, fee = lot.amount, lot.cost_basis - lot.amount
        else:
            amt = math.floor(lot.amount * take / lot.shares + 1e-6)
            fee = math.floor((lot.cost_basis - lot.amount) * take / lot.shares + 1e-6)
        pieces.append((lot, take, amt, fee))
        lot.amount -= amt
        lot.cost_basis -= amt + fee
        lot.shares -= take
        left -= take
    lots[:] = [x for x in lots if x.shares > 0]
    return pieces


def _consume_fifo(pos: Position, shares: int, odd: bool = False) -> float:
    """賣出部分的成本（成交價金＋買進手續費），扣法見 take_lots。"""
    return sum(amt + fee for _, _, amt, fee in take_lots(pos.lots, shares, odd))


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


def app_total(pf: Portfolio, prices: dict[str, float]) -> float:
    """競賽 App「投資組合總值」＝ 初始資金 ＋ 未實現 ＋ 已實現（手續費不從總值扣）。
    現股未實現＝市值 − 成交價金；期貨未實現用 prices 中的合約價（收盤後為結算價）。"""
    sec = sum(p.shares * prices.get(c, p.avg_cost) - sum(lot.amount for lot in p.lots) for c, p in pf.positions.items())
    fut = 0.0
    for c, d in pf.derivs.items():
        px = prices.get(c, d.avg_price)
        fut += d.value(px) if d.kind == "FUT" else (px - d.avg_price) * d.multiplier * d.qty
    return rules()["capital"]["initial"] + sec + fut + pf.realized_pnl
