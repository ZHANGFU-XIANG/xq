"""讀取每日報告（facts JSON）與投資組合狀態，整理成前端／AI 助理需要的格式。"""
from __future__ import annotations

import json
from functools import lru_cache

import pandas as pd

from src.agents import on_demand
from src.config import FACTS_DIR, REPORT_DIR, now_tw, rules, stock_meta
from src.state import params
from src.state import portfolio as pfm


def report_files() -> list[dict]:
    out = []
    for fp in sorted(FACTS_DIR.glob("*.json"), reverse=True):
        date, title = fp.stem.split("_", 1)
        out.append({"date": date, "title": title, "key": fp.stem})
    return out


@lru_cache(maxsize=32)
def _load(key: str, mtime: float) -> dict:
    return json.loads((FACTS_DIR / f"{key}.json").read_text(encoding="utf-8"))


def load_report(key: str | None = None) -> dict | None:
    files = report_files()
    if not files:
        return None
    if key is None:
        pre = [f for f in files if f["title"] == "盤前報告"]
        key = (pre or files)[0]["key"]
    elif len(key) == 10:  # 只給日期 → 該日盤前報告優先
        same = [f for f in files if f["date"] == key]
        if not same:
            return None
        key = next((f["key"] for f in same if f["title"] == "盤前報告"), same[0]["key"])
    fp = FACTS_DIR / f"{key}.json"
    if not fp.exists():
        return None
    data = _load(key, fp.stat().st_mtime)
    stem = fp.stem
    month = data["date"][:7]
    data = dict(data)
    data["files"] = {ext: f"{month}/{stem}{suffix}" for ext, suffix in
                     (("docx", ".docx"), ("pptx", ".pptx"), ("csv", "_下單清單.csv"))
                     if (REPORT_DIR / month / f"{stem}{suffix}").exists()}
    data["key"] = key
    return data


def report_summary(date: str | None = None) -> dict:
    r = load_report(date)
    if not r:
        return {"error": "查無報告"}
    return {
        "date": r["date"], "title": r["title"], "allocation": {k: r["allocation"][k] for k in ("offense", "defense", "reasons")},
        "taiex_prediction": r["predictions"].get("TAIEX"),
        "orders": [{k: o.get(k) for k in ("action", "basket_name", "code", "name", "shares", "lots", "odd", "limit_price",
                                          "stop", "amount", "weight_pct", "why_sell")} for o in r["orders"]],
        "compliance": r["compliance"], "top_picks": r["top_picks"][:10], "def_picks": r.get("def_picks", [])[:8],
        "summary": r.get("text", {}).get("market_summary"), "outlook": r.get("text", {}).get("outlook"),
    }


def _prices(codes: list[str]) -> dict[str, float]:
    """即時價（盤中為最新成交、盤後為今日收盤）；取不到才用日 K 最後一筆。"""
    from src.agents import alerts

    rt = alerts.realtime(codes) if codes else {}
    prices = {c: q["price"] for c, q in rt.items() if q.get("price")}
    for c in codes:
        if c in prices:
            continue
        df = on_demand.price_df(c)
        if not df.empty:
            prices[c] = float(df.Close.iloc[-1])
    return prices


def app_view(pf, sec_unreal: float, fut_unreal: float, marks: dict) -> dict:
    """競賽 App「證券績效」頁的算法（10/06 對帳一致：現金 966,764、未實現 191,866、總值 10,191,866、報酬 1.9187%）：
    - 投資組合總值 ＝ 初始資金 ＋ 未實現 ＋ 已實現（手續費不從總值扣）；投資報酬率 ＝ 總值 ÷ 初始 − 1
    - 現股未實現 ＝ 市值 − 成交價金（每筆捨去）；期貨未實現用日盤結算價
    - 持有現金 ＝ 證券戶現金 ＋ 期貨戶（現金 − 原始保證金 ＋ 未實現）
    已實現損益尚無賣出紀錄可對帳（暫以扣除手續費與稅後計）。"""
    from src.agents import futures_data

    initial = rules()["capital"]["initial"]
    margin = 0.0
    if pf.derivs:
        try:
            margin = pf.margin_required(futures_data.margins())
        except Exception:  # noqa: BLE001
            margin = 0.0
    unreal = sec_unreal + fut_unreal
    realized = pf.realized_pnl
    total = initial + unreal + realized
    return {"total": round(total), "return_pct": round((total / initial - 1) * 100, 4),
            "unrealized": round(unreal), "unrealized_sec": round(sec_unreal), "unrealized_fut": round(fut_unreal),
            "realized": round(realized), "cash": round(pf.cash + pf.futures_equity(marks) - margin),
            "margin": round(margin), "fees_paid": round(sum(t["cost"] for t in pf.trades))}


def portfolio() -> dict:
    from src.agents import alerts, compliance

    pf = pfm.build_portfolio()
    meta = pfm.load_meta()
    prices = _prices(list(pf.positions))
    marks = {}
    derivs = []
    if pf.derivs:
        from src.agents import futures_data

        marks = futures_data.marks(pf)
        margins = futures_data.margins()
        for c, d in pf.derivs.items():
            px = marks.get(c)
            derivs.append({"contract": c, "root": d.root, "kind": "期貨" if d.kind == "FUT" else "賣權（買方）",
                           "qty": d.qty, "avg_price": round(d.avg_price, 2), "price": px,
                           "value": round(d.value(px)), "multiplier": d.multiplier,
                           "margin": round(abs(d.qty) * margins.get(d.root, {}).get("initial", 0)) if d.kind == "FUT" else 0})
    nav = pf.nav({**prices, **marks})
    pos = []
    for c, p in pf.positions.items():
        price = prices.get(c, p.avg_cost)
        mv = p.shares * price
        gross = sum(lot.amount for lot in p.lots)  # 成交價金（每筆捨去、不含手續費），與競賽 App 的成本相同
        pos.append({"code": c, "name": stock_meta(c)["name"], "shares": p.shares, "avg_cost": round(p.avg_cost, 2),
                    "price": price, "market_value": round(p.shares * price), "pnl": round(mv - gross),  # 同競賽 App「未實現損益」：市值 − 成交價金（手續費不計）
                    "pnl_pct": round((mv / gross - 1) * 100, 2) if gross else 0, "cost_amount": round(gross),
                    "pnl_after_fee": round(mv - p.cost_basis),
                    "weight_pct": round(p.shares * price / nav * 100, 2) if nav else 0,
                    "asset": "ETF" if stock_meta(c).get("is_etf") else "STOCK",
                    "basket": params.basket("etf" if stock_meta(c).get("is_etf") else (meta.get(c, {}).get("basket") or "offense"))["name"],
                    "stop": meta.get(c, {}).get("stop"), "margin_loan": round(p.margin_loan),
                    # 每一批成交各自的成本與損益（整股／零股、不同時間成交分開算）
                    "lots": [{"date": lot.date, "shares": lot.shares, "price": lot.price, "unit_cost": round(lot.unit_cost, 3),
                              "board": "整股" if lot.shares % 1000 == 0 else "零股" if lot.shares < 1000 else "整股＋零股",
                              "pnl": round(lot.shares * (price - lot.price)),
                              "pnl_pct": round((price / lot.price - 1) * 100, 2) if lot.price else 0,
                              "note": lot.note}
                             for lot in p.lots]})
    initial = rules()["capital"]["initial"]
    # 先加總再四捨五入（與 App 相同；逐檔四捨五入再加會差 1 元）
    unreal = sum(p.shares * prices.get(c, p.avg_cost) - sum(lot.amount for lot in p.lots) for c, p in pf.positions.items())
    fut_unreal = sum(x["value"] if x["root"] != "TXO" else
                     ((x["price"] or x["avg_price"]) - x["avg_price"]) * x["multiplier"] * x["qty"] for x in derivs)
    stock_mv = sum(x["market_value"] for x in pos)
    app = app_view(pf, unreal, fut_unreal, marks)
    return {"nav": app["total"], "return_pct": app["return_pct"], "cash": round(pf.cash), "app": app,
            # nav／return_pct 採競賽 App「投資組合總值／投資報酬率」口徑（初始 ＋ 未實現 ＋ 已實現，手續費不扣）
            # nav_net 為實際淨值（現金 ＋ 市值 ＋ 期貨權益，已扣手續費）
            "nav_net": round(nav), "return_net_pct": round((nav / initial - 1) * 100, 3),
            "holding_cost": round(sum(x["cost_amount"] for x in pos)), "unrealized_pnl": round(unreal),
            "holding_return_pct": round(unreal / initial * 100, 4), "fees_paid": round(sum(t["cost"] for t in pf.trades)),
            # 總資產組成（證券市值含股票與 ETF；期貨戶權益含期貨未實現與選擇權市值）
            "breakdown": {"stock_mv": round(sum(x["market_value"] for x in pos if x["asset"] == "STOCK")),
                          "etf_mv": round(sum(x["market_value"] for x in pos if x["asset"] == "ETF")),
                          "cash": round(pf.cash), "futures_equity": round(pf.futures_equity(marks)),
                          "margin_loan": round(pf.margin_loan())},
            "futures_unrealized": round(fut_unreal), "unrealized_total": round(unreal + fut_unreal),
            "securities_mv": round(stock_mv),
            "futures_cash": round(pf.futures_cash), "futures_equity": round(pf.futures_equity(marks)), "derivatives": derivs,
            "holding_ratio": round(pf.holding_ratio({**prices, **marks}), 4),
            "margin_loan": round(pf.margin_loan()), "realized_pnl": round(pf.realized_pnl), "trade_count": pf.trade_count,
            "traded_symbols": sorted(pf.traded_symbols), "symbol_count": len(pf.traded_symbols), "positions": pos,
            "compliance": [c for c in compliance.check(pf, {**prices, **marks}, [], now_tw().date(), nav)
                           if not c["item"].startswith("執行建議後")],
            "as_of": now_tw().strftime("%H:%M:%S"), "trades": pf.trades[-50:][::-1],
            "nav_history": pfm.load_history()}


# ---------------------------------------------------------------- 交易紀錄（手動新增／修改／刪除）與統計
SIDE_LABEL = {"BUY": "買進", "SELL": "賣出", "ADJ": "手續費校正", "TO_FUTURES": "轉入期貨戶", "TO_SECURITIES": "轉回證券戶"}


def trade_ledger() -> dict:
    """全部交易紀錄（id＝在 trades.csv 的順序）＋統計分析。"""
    from src.agents import trade_review

    rows = pfm.load_trades()
    pf = pfm.build_portfolio(rows)
    out = []
    for i, t in enumerate(rows):
        code, side = t["code"].strip(), t["side"].strip().upper()
        kind = _kind_of(t)
        item = {"id": i, **{k: t.get(k) or "" for k in pfm.TRADE_FIELDS}, "kind": kind, "side_label": SIDE_LABEL.get(side, side)}
        if kind in ("STOCK", "ETF"):
            shares, price = int(float(t["shares"])), float(t["price"])
            fee, tax = trade_review.fee_tax(code, side, shares, price)
            item.update(name=stock_meta(code).get("name", ""), amount=round(shares * price), fee=fee, tax=tax,
                        board="整股" if shares % 1000 == 0 else "零股" if shares < 1000 else "整股＋零股")
        elif kind in ("FUT", "OPT"):
            qty, price = int(float(t["shares"])), float(t["price"])
            item.update(name=pfm.deriv_spec(code)["name"], fee=pfm.deriv_cost(code, qty, price), tax=0.0,
                        amount=round(price * pfm.deriv_spec(code)["multiplier"] * qty))  # 期貨＝契約價值、選擇權＝權利金
        out.append(item)
    stats = trade_review.review(rows, _prices(list({t["code"].strip() for t in rows if pfm.asset_of(t) in ("STOCK", "ETF")
                                                   and t["code"].strip().upper() not in ("FEE", "TRANSFER")})))
    marks = {}
    if pf.derivs:
        try:
            from src.agents import futures_data

            marks = futures_data.marks(pf)
        except Exception:  # noqa: BLE001  期交所行情暫時取不到時以均價計（未實現 0）
            marks = {}
    fut = trade_review.review_derivs(rows, marks)
    sm = stats["summary"]
    sm["deriv_realized"] = fut["realized"]
    stats["derivs"] = fut
    # 證券（股票＋ETF）／期貨（期貨＋選擇權）／全部 的損益與交易概況，前端篩選用
    sec = {"realized": sm["realized"], "unrealized": sm["unrealized"], "total_pnl": sm["total_pnl"],
           "fees": sm["fee"] + sm["tax"], "trade_count": sm["stock_trades"], "symbols": sm["symbols"]}
    futs = {"realized": fut["realized"], "unrealized": fut["unrealized"], "total_pnl": fut["total_pnl"],
            "fees": fut["fees"], "trade_count": fut["trade_count"], "symbols": len(fut["rows"])}
    stats["segments"] = {"sec": sec, "fut": futs,
                         "all": {k: sec[k] + futs[k] for k in sec}}
    return {"version": pfm.trades_version(rows), "rows": out[::-1],  # 最新在前
            "holdings": [{"code": c, "name": stock_meta(c).get("name", ""), "shares": p.shares} for c, p in pf.positions.items()],
            "derivs": [{"contract": c, "root": d.root, "kind": d.kind, "qty": d.qty, "avg_price": round(d.avg_price, 2)}
                       for c, d in pf.derivs.items()],
            "cash": round(pf.cash), "futures_cash": round(pf.futures_cash), "stats": stats}


ASSET_KINDS = ("STOCK", "ETF", "FUT", "OPT", "TRANSFER", "CASH")


def _kind_of(row: dict) -> str:
    code = str(row.get("code", "")).strip().upper()
    return {"FEE": "CASH", "TRANSFER": "TRANSFER"}.get(code) or pfm.asset_of(row)


def _clean_trade(t: dict, base: dict | None = None) -> dict:
    """驗證並整理一筆手動輸入的紀錄；base 為修改前的那一列。

    t["kind"]：STOCK／ETF（股數、成交價）、FUT（code=TX/MTX/TMF、contract=TMF202610、口數、點數）、
    OPT（code=TXO、contract=TXO202610P46000、口數、權利金點數，只做買方）、
    TRANSFER（side=TO_FUTURES/TO_SECURITIES、price=金額）、CASH（手續費校正，price=金額，可為負）。
    """
    import datetime as dt
    import re

    from src.agents.compliance import is_warrant

    base = base or {}
    row = {k: base.get(k, "") or "" for k in pfm.TRADE_FIELDS}
    kind = str(t.get("kind") or (_kind_of(row) if base else "STOCK")).strip().upper()
    if kind not in ASSET_KINDS:
        raise ValueError(f"未知類別 {kind}")
    try:
        d = dt.date.fromisoformat(str(t.get("date", row["date"])).strip())
    except ValueError as e:
        raise ValueError("日期格式要是 2026-10-06") from e
    if d > now_tw().date():
        raise ValueError("日期不能是未來")
    try:
        shares = float(t.get("shares") or row["shares"] or 1)
        price = float(t.get("price", row["price"]))
    except (TypeError, ValueError) as e:
        raise ValueError("數量與價格要是數字") from e
    code = str(t.get("code", row["code"])).strip().upper()
    side = str(t.get("side", row["side"])).strip().upper()
    contract = str(t.get("contract", row["contract"]) or "").strip().upper()
    margin = str(t.get("margin", row["margin"])).strip().lower() in ("1", "true", "y")

    if kind == "TRANSFER":
        if side not in ("TO_FUTURES", "TO_SECURITIES"):
            raise ValueError("資金調撥方向只能是「證券戶 → 期貨戶」或「期貨戶 → 證券戶」")
        if price <= 0:
            raise ValueError("調撥金額要大於 0")
        code, shares, contract, margin, asset = "TRANSFER", 1, "", False, ""
    elif kind == "CASH":
        if price == 0:
            raise ValueError("校正金額不能是 0（正數＝多付的手續費，負數＝退回）")
        code, side, shares, contract, margin, asset = "FEE", "ADJ", 1, "", False, "CASH"
    else:
        if side not in ("BUY", "SELL"):
            raise ValueError("買賣方向只能是買進或賣出")
        if shares <= 0 or shares != int(shares):
            raise ValueError("數量要是正整數（股票 1 張 = 1000 股；期貨、選擇權為口數）")
        if price <= 0:
            raise ValueError("價格要大於 0")
        asset = kind
        if kind in ("STOCK", "ETF"):
            if not re.fullmatch(r"\d{4,6}[A-Z]?", code):
                raise ValueError(f"股票代號格式不對：{code}")
            if is_warrant(code):
                raise ValueError("權證不可交易（競賽規則）")
            from src.agents.universe import stock_info

            info = stock_info()
            if info and code not in info:  # 股票清單載入失敗時不擋
                raise ValueError(f"查無代號 {code}（請確認是上市櫃股票或 ETF）")
            asset = "ETF" if pfm.is_etf(code) else "STOCK"  # 依代號判斷，避免選錯類別讓證交稅算錯
            contract = ""
            cap = rules()["capital"]["initial"]
            if shares * price > cap:  # 例：10 張被當成 10,000 張（多乘 1000）
                raise ValueError(f"{code} {int(shares):,} 股 × {price:g} = {shares * price:,.0f} 元，超過競賽本金 {cap:,.0f} 元，"
                                 "可能把張數當成股數多乘了 1000，請核對數量")
        elif kind == "FUT":
            if code not in ("TX", "MTX", "TMF"):
                raise ValueError("期貨商品只能是 TX 大台、MTX 小台、TMF 微台")
            if not re.fullmatch(code + r"20\d{2}(0[1-9]|1[0-2])", contract):
                raise ValueError(f"期貨合約格式要是 {code}202610（商品＋年月）")
            margin = False
        else:  # OPT
            code = "TXO"
            if not re.fullmatch(r"TXO20\d{2}(0[1-9]|1[0-2])[CP]\d{3,6}", contract):
                raise ValueError("選擇權合約格式要是 TXO202610P46000（年月＋C 買權／P 賣權＋履約價）")
            margin = False
    row.update(date=d.isoformat(), code=code, side=side, shares=str(int(shares)),
               price=f"{price:.6f}".rstrip("0").rstrip("."), margin="1" if margin else "0", asset=asset, contract=contract,
               note=" ".join(str(t.get("note", row["note"]) or "").replace(",", "，").split())[:80])
    return row


def _desc(r: dict) -> str:
    kind = _kind_of(r)
    side = SIDE_LABEL.get(r["side"].upper(), r["side"])
    if kind == "TRANSFER":
        return f"{r['date']} {side} {float(r['price']):,.0f} 元"
    if kind == "CASH":
        return f"{r['date']} 手續費校正 {float(r['price']):,.0f} 元"
    if kind in ("FUT", "OPT"):
        return f"{r['date']} {side} {r['contract']} {r['shares']} 口 @ {r['price']}"
    return f"{r['date']} {side} {r['code']} {r['shares']} 股 @ {r['price']}"


DELETE_CONFIRM = "確認刪除"


def _stock_positions(rows: list[dict]) -> dict[str, int]:
    try:
        return {c: p.shares for c, p in pfm.build_portfolio(rows).positions.items()}
    except ValueError:
        return {}


def _guard_delete(before: list[dict], after: list[dict], confirm: str | None) -> None:
    """Prevent a mass delete like 10/07: if stock holdings would drop to zero, or at least half of the stock rows are deleted,
    the request must carry confirm="確認刪除"."""
    if confirm == DELETE_CONFIRM:
        return
    is_stock = lambda r: pfm.asset_of(r) in ("STOCK", "ETF")  # noqa: E731
    n_before = sum(map(is_stock, before))
    n_gone = n_before - sum(map(is_stock, after))
    pos_before, pos_after = _stock_positions(before), _stock_positions(after)
    lost = [c for c in pos_before if c not in pos_after]
    if (pos_before and not pos_after) or (n_before >= 4 and n_gone >= n_before / 2):
        names = "、".join(f"{c} {stock_meta(c)['name']}" for c in lost[:8]) + (" 等" if len(lost) > 8 else "")
        raise ValueError(f"這次會刪除 {n_gone} 筆股票紀錄（共 {n_before} 筆）"
                         + (f"，並清掉 {len(lost)} 檔持股：{names}" if lost else "")
                         + f"。確定要刪除，請輸入「{DELETE_CONFIRM}」；刪錯可以在「歷史版本」復原")


def trade_mutate(op: str, version: str, trade: dict | None = None, tid: int | None = None,
                 trades: list[dict] | None = None, ids: list[int] | None = None, confirm: str | None = None) -> dict:
    """op：add／update／delete／bulk_add（trades，一次 commit）／bulk_delete（ids，一次 commit）。
    驗證後整份改寫（雲端 commit 到 GitHub），回傳新版本與說明。"""
    from src.api import sync

    rows = pfm.load_trades()
    before = list(rows)
    if op in ("update", "delete") and (tid is None or not 0 <= tid < len(rows)):
        raise ValueError("找不到這筆交易，請重新整理")
    if op == "bulk_add":
        if not trades:
            raise ValueError("沒有要新增的紀錄")
        new = []
        for i, t in enumerate(trades, 1):
            try:
                new.append(_clean_trade(t))
            except ValueError as e:
                raise ValueError(f"第 {i} 筆（{t.get('code', '')}）：{e}") from e
        rows = rows + new
        what = f"匯入 {len(new)} 筆（{'、'.join(_desc(n) for n in new[:3])}{' 等' if len(new) > 3 else ''}）"
    elif op == "bulk_delete":
        sel = sorted(set(ids or []))
        if not sel or any(not 0 <= i < len(rows) for i in sel):
            raise ValueError("選取的紀錄不存在，請重新整理")
        gone = [rows[i] for i in sel]
        rows = [r for i, r in enumerate(rows) if i not in set(sel)]
        what = f"刪除 {len(gone)} 筆（{'、'.join(_desc(g) for g in gone[:3])}{' 等' if len(gone) > 3 else ''}）"
    elif op == "add":
        new = _clean_trade(trade or {})
        rows = rows + [new]
        what = f"新增 {_desc(new)}"
    elif op == "update":
        old = rows[tid]
        new = _clean_trade(trade or {}, old)
        rows = rows[:tid] + [new] + rows[tid + 1:]
        what = f"修改 {_desc(old)} → {_desc(new)}"
    elif op == "delete":
        old = rows[tid]
        rows = rows[:tid] + rows[tid + 1:]
        what = f"刪除 {_desc(old)}"
    else:
        raise ValueError(f"未知操作 {op}")
    rows = sorted(rows, key=lambda r: r["date"])  # 依日期重播（同一天維持原順序），先進先出才正確
    if op in ("delete", "bulk_delete"):
        _guard_delete(before, rows, confirm)
    try:
        msg = sync.replace_trades(rows, f"manual trade edit: {what}", version)
    except ValueError as e:
        raise ValueError(f"無法寫入：{e}") from e
    return {"ok": True, "message": f"已{what}。{msg}", "version": pfm.trades_version(rows)}


def _oversized(rows: list[dict]) -> list[str]:
    """Stock rows whose single-fill amount exceeds the competition capital (e.g. lots multiplied by 1000 twice on 10/07)."""
    cap = rules()["capital"]["initial"]
    return [f"{r['date']} {r['code']} {int(float(r['shares'])):,} 股" for r in rows
            if pfm.asset_of(r) in ("STOCK", "ETF") and float(r["shares"] or 0) * float(r["price"] or 0) > cap]


_HIST_CACHE: dict[str, dict] = {}  # sha → 該版本摘要（commit 內容不會變，可以永久快取）


def trades_history(n: int = 15) -> dict:
    """data/trades.csv 的歷史版本（每次寫入都是一個 commit），附各版本的筆數與股票持股檔數。"""
    from src.api import sync

    out = []
    for c in sync.trades_history(n):
        if c["sha"] not in _HIST_CACHE:
            try:
                rows = sync.trades_at(c["sha"])
                pos = _stock_positions(rows)
                _HIST_CACHE[c["sha"]] = {"rows": len(rows), "holdings": len(pos), "oversized": _oversized(rows)[:3],
                                         "holding_names": [f"{k} {stock_meta(k)['name']}" for k in sorted(pos)][:12]}
            except Exception as e:  # noqa: BLE001
                _HIST_CACHE[c["sha"]] = {"rows": None, "holdings": None, "error": str(e)[:80]}
        out.append({**c, **_HIST_CACHE[c["sha"]]})
    return {"versions": out, "current_version": pfm.trades_version()}


def trade_restore(sha: str, version: str) -> dict:
    """把交易紀錄整份復原成某個歷史版本（本身也是一個新的 commit，可以再復原回來）。"""
    from src.api import sync

    rows = sync.trades_at(sha)
    if not rows:
        raise ValueError("這個版本沒有交易紀錄，不能復原")
    bad = _oversized(rows)
    if bad:
        raise ValueError(f"這個版本有數量異常的紀錄（{'、'.join(bad[:3])}，單筆金額超過本金），不能復原")
    msg = sync.replace_trades(sorted(rows, key=lambda r: r["date"]), f"restore trades to {sha[:7]}", version)
    return {"ok": True, "message": f"已復原成 {sha[:7]} 版本（{len(rows)} 筆）。{msg}", "version": pfm.trades_version(rows)}


def macro_latest() -> dict:
    r = load_report()
    if not r:
        return {"error": "查無報告"}
    return {"date": r["date"], "events": r.get("macro", []), "analysis": r.get("text", {}).get("macro_analysis"),
            "hypothetical": r.get("hypothetical")}


_TRACK: dict = {"t": 0.0, "v": None}


def track_record() -> dict:
    """實際表現（含回補歷史）：依預測紀錄即時對答案，30 分鐘快取。"""
    import time

    import pandas as pd

    from src.agents import tuner

    if _TRACK["v"] is not None and time.time() - _TRACK["t"] < 1800:
        return _TRACK["v"]
    ctx = on_demand.context()
    ev = tuner.evaluate(ctx["panel"], ctx["taiex"])
    monthly = []
    if ev["alpha"]:
        df = pd.DataFrame(ev["alpha"])
        df["month"] = pd.to_datetime(df["target_date"]).dt.strftime("%Y-%m")
        for (b, m), g in df.groupby(["basket", "month"]):
            monthly.append({"basket": b, "month": m, "n": int(len(g)), "avg_ret": round(float(g.real_ret.mean()), 4),
                            "avg_excess": round(float(g.real_excess.mean()), 4), "up_rate": round(float((g.real_ret > 0).mean()), 3),
                            "newhigh_rate": round(float(g.real_newhigh.mean()), 3)})
    out = {"stats": tuner.summary_stats(ev), "monthly": monthly,
           "backfill_note": "包含回補的歷史預測（模型只用當天以前的資料訓練，不偷看未來）"}
    _TRACK.update(t=time.time(), v=out)
    return out


def model_health() -> dict:
    from src.agents import strategy_tuner, tuner

    r = load_report() or {}
    am = r.get("alpha_model", {})
    tr = track_record()
    return {"date": r.get("date"), "backtest": am.get("metrics"), "base_rate": am.get("base_rate"),
            "n_stocks": am.get("n_stocks"), "feature_ic": am.get("feature_ic"),
            "live": tr["stats"], "monthly": tr["monthly"], "backfill_note": tr["backfill_note"],
            "tuning_log": tuner.history()[-10:], "learned_params": params.learned(),
            "strategy_scorecard": strategy_tuner.scorecard()}


def news(date: str | None = None) -> dict:
    r = load_report(date)
    if not r:
        return {"news": [], "events": [], "macro": []}
    from src.agents.news import is_finance

    def keep(events):  # 事件只保留財經新聞佐證；全部被濾掉的事件不顯示
        out = []
        for e in events or []:
            ns = [n for n in e.get("news", []) if is_finance(n)]
            if ns or not e.get("news"):
                out.append({**e, "news": ns})
        return out

    from src.agents.news import asset_category

    return {"date": r["date"], "news": [{**n, "category": n.get("category") or asset_category(n)} for n in r.get("news", [])
                                        if is_finance(n)], "events": keep(r.get("events")),
            "macro": keep(r.get("macro")),
            "market_view": r.get("market_view"), "us_snapshot": r.get("us_snapshot", []),
            "market_summary": (r.get("text") or {}).get("market_summary")}


_ALERTS: dict = {"t": 0.0, "v": None}


_ALERTS_LOCK = __import__("threading").Lock()


def live_alerts(refresh: bool = False) -> dict:
    """即時警示＋每則持股警示附上「持股建議的最終結論」（同一套判斷），警示頁與持股建議、個股頁說法一致。"""
    out = _live_alerts(refresh)
    iv = _INTRA.get("v") or {}
    items = {h["code"]: h for h in (iv.get("holding_items") or [])}
    if not items:
        return out
    src = out.get("alerts") or []
    if iv.get("holding_alerts") is not None:
        # 持股警示改用盤中建議同一批算出來的（持股建議也是用這批合併賣單），兩邊不會因為計算時間不同而說法不一
        rank = {"嚴重": 0, "警告": 1, "提醒": 2}
        src = sorted(iv["holding_alerts"] + [a for a in src if a.get("scope") != "持股"], key=lambda a: rank.get(a.get("level"), 9))
        out = {**out, "counts": {lv: sum(1 for a in src if a.get("level") == lv) for lv in rank}}
    alerts = []
    for a in src:
        if isinstance(a.get("price"), float):
            a = {**a, "price": round(a["price"], 2)}  # 盤中那批的價格是浮點數（例如 12.949999809），顯示前取到小數 2 位
        h = items.get(a.get("code")) if a.get("scope") == "持股" else None
        if h:
            a = {**a, "final": {"verdict": h["verdict"], "text": f"{h['verdict']}：{h['qty_text']}"
                                + (f"，限價 {h['limit_price']}" if h.get("limit_price") else ""),
                                "reasons": h.get("reasons") or []}}
        alerts.append(a)
    return {**out, "alerts": alerts}


def _live_alerts(refresh: bool = False) -> dict:
    """即時警示（5 分鐘快取）：持股與今日建議以盤中即時價判斷，全市場漲跌停用最近收盤。"""
    import time

    from src.agents import alerts

    ttl = 60 if alerts.is_trading_hours() else 300  # 盤中每分鐘（跌停、跌破停損要即時）；新聞判讀與注意股名單另有快取
    if not refresh and _ALERTS["v"] is not None and time.time() - _ALERTS["t"] < ttl:
        return _ALERTS["v"]
    r = load_report() or {}

    def compute():
        with _ALERTS_LOCK:
            out = alerts.collect(pfm.build_portfolio(), pfm.load_meta(), r.get("orders", []), r.get("us_snapshot", []),
                                 on_demand.current_target())
            _ALERTS.update(t=time.time(), v=out)
            return out

    prev = _ALERTS["v"] or ((r.get("alerts") or None) and {**r["alerts"], "realtime": {}, "from_report": r.get("date")})
    if refresh or prev is None:
        return compute()
    if not _ALERTS_LOCK.locked():  # 先回上一份（或報告內的警示），背景更新
        __import__("threading").Thread(target=compute, daemon=True).start()
    return {**prev, "stale": True}


_INTRA: dict = {"slot": None, "key": None, "v": None}
_INTRA_ERR: dict = {"t": 0.0, "msg": None}
_INTRA_LOCK = __import__("threading").Lock()


def _saved_intraday() -> dict | None:
    """雲端排程每 40 分鐘存下的最新一份盤中建議（data/intraday/*.json，經 GitHub 同步到網站）。"""
    from src.config import DATA_DIR

    files = sorted((DATA_DIR / "intraday").glob("*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _compute_intraday(slot: str, key: tuple) -> dict:
    from src.agents import intraday

    with _INTRA_LOCK:  # 同一時間只算一份（全市場重算約 1～1.5 分鐘）
        if _INTRA["v"] is not None and _INTRA["slot"] == slot and _INTRA["key"] == key and not _INTRA["v"].get("stale"):
            return _INTRA["v"]
        try:
            out = intraday.run(load_report())
        except Exception as e:  # noqa: BLE001 - 記下錯誤並冷卻 2 分鐘，避免前端每 10 秒輪詢時一直重開全市場重算
            import logging
            import time

            _INTRA_ERR.update(t=time.time(), msg=str(e)[:160])
            logging.getLogger(__name__).exception("盤中建議重算失敗")
            raise
        _INTRA_ERR.update(t=0.0, msg=None)
        _INTRA.update(slot=slot, key=key, v=out)
        return out


LIVE_REFRESH_MIN = 1  # 網站盤中每 1 分鐘以即時價重算（2026-10-11 使用者要求；一次約 5～30 秒）；雲端排程與 Email 仍為每 40 分鐘一輪


def _live_slot() -> str:
    from src.agents import alerts, intraday

    slot = intraday.current_slot()
    if alerts.is_trading_hours():
        n = now_tw()
        slot += f" #{n.hour:02d}:{n.minute // LIVE_REFRESH_MIN * LIVE_REFRESH_MIN:02d}"
    return slot


def live_intraday(refresh: bool = False) -> dict:
    """盤中建議：網站盤中每 1 分鐘以即時價重算（雲端排程每 40 分鐘一輪並寄信）；持股變動或手動重算時立即更新。
    還沒有這個時段的結果時，先回傳上一份（記憶體或雲端存檔）並標示 stale，背景重算完自動替換，不讓使用者等。"""
    import threading

    slot = _live_slot()
    pf = pfm.build_portfolio()
    key = (tuple(sorted((c, p.shares) for c, p in pf.positions.items())), round(pf.cash))  # 成本或現金改正也要重算
    fresh = _INTRA["v"] is not None and _INTRA["slot"] == slot and _INTRA["key"] == key
    if fresh and not refresh:
        return _INTRA["v"]
    if refresh:
        _INTRA["v"] = None
        return _compute_intraday(slot, key)
    prev = _INTRA["v"] or _saved_intraday()
    if prev is None:
        return _compute_intraday(slot, key)
    import time

    if not _INTRA_LOCK.locked() and time.time() - _INTRA_ERR["t"] > 120:
        threading.Thread(target=_compute_intraday, args=(slot, key), daemon=True).start()
    out = {**prev, "stale": True, "cached_from": prev.get("time")}
    if _INTRA_ERR["msg"]:
        out["error"] = f"重算失敗，2 分鐘後重試：{_INTRA_ERR['msg']}"
    return out


_WATCH: dict = {"t": 0.0, "v": None}


def _live_rsi(close, price: float, n: int = 14) -> float | None:
    """歷史收盤＋即時價計算 RSI（與模型特徵相同的算法，門檻才一致）。"""
    import pandas as pd

    from src.agents.alpha_model import _rsi

    s = pd.concat([close.tail(120), pd.Series([price])], ignore_index=True)
    v = _rsi(s, n).iloc[-1]
    return None if pd.isna(v) else float(v)


def _deriv_watch(pf) -> list[dict]:
    """期貨／選擇權部位：即時未實現損益、距停損（取盤中建議同合約的停損價）、保證金是否吃緊。"""
    if not pf.derivs:
        return []
    from src.agents import futures_data

    marks = futures_data.marks(pf)
    margins = futures_data.margins()
    plan = {o.get("contract"): o for o in ((_INTRA.get("v") or {}).get("derivatives") or {}).get("orders", [])}
    eq = pf.futures_equity(marks)
    maint = pf.margin_required(margins, "maintenance")
    out = []
    for c, d in pf.derivs.items():
        px = marks.get(c)
        pnl = d.value(px) if d.kind == "FUT" else d.value(px) - d.avg_price * d.multiplier * d.qty
        stop = (plan.get(c) or {}).get("stop")
        reasons, status = [], "正常"
        if d.kind == "FUT" and px and stop and ((d.qty > 0 and px <= stop) or (d.qty < 0 and px >= stop)):
            status, reasons = "盡快賣出", [f"現價 {px:,.0f} 已觸及停損 {stop:,.0f} 點，平倉"]
        elif d.kind == "FUT" and maint and eq < maint * 1.1:
            status, reasons = "盡快賣出", [f"期貨戶權益數 {eq:,.0f} 元接近維持保證金 {maint:,.0f} 元，補錢或減口數避免追繳"]
        elif d.kind == "FUT" and px and stop and abs(px - stop) / px < 0.01:
            status, reasons = "注意", [f"距停損 {stop:,.0f} 點不到 1%"]
        out.append({"code": c, "name": f"{d.root} {'多' if d.qty > 0 else '空'} {abs(d.qty)} 口", "status": status, "reasons": reasons,
                    "price": px, "chg_pct": None, "pnl_pct": None, "pnl": round(pnl), "stop": stop, "rsi14": None,
                    "p_up": None, "exp_ret": None, "verdict": None, "asset": "FUT"})
    return out


_REGIME: dict = {"t": 0.0, "v": None}


def live_regime() -> dict:
    """大盤攻守燈號（綠／灰／黃／紅）＋撤資預警；盤中用即時加權指數預判今天收盤會不會變燈。60 秒快取。"""
    import time

    from src.agents import regime

    if _REGIME["v"] is not None and time.time() - _REGIME["t"] < 60:
        return _REGIME["v"]
    ctx = on_demand.context()
    tx = ctx["taiex"]
    tx = tx[tx.index < pd.Timestamp(now_tw().date())]  # 只用到昨天收盤的日 K，今天的看即時價
    live = None
    try:
        live = (live_quotes([]).get("indices") or {}).get("TAIEX", {}).get("price")
    except Exception:  # noqa: BLE001
        pass
    r = load_report() or {}
    out = regime.assess(tx, ctx.get("us_hist"), live_price=live, events=r.get("macro"))
    out["live_taiex"] = live
    out["time"] = now_tw().strftime("%H:%M:%S")
    _REGIME.update(t=time.time(), v=out)
    return out


def holding_watch() -> dict:
    """持股動向（盤中每分鐘）：每檔庫存判斷「盡快賣出／低檔可望反彈／注意／正常」，規則全由程式計算。

    - 盡快賣出：跌破停損、跌停、持股建議為出清或減碼、或今日重挫且模型一個月收正機率低於全市場平均
    - 低檔可望反彈：超賣（RSI ≤ 35、貼近 10 日低點或今日跌 ≥3%）且模型一個月收正機率明顯高於平均、預期報酬為正、未破停損
      → 建議續抱、不要殺低（機率是模型估計，不是保證）
    - 注意：今日跌 ≥3% 或跌破 20 日均線，但還沒到賣出條件
    """
    import time

    from src.agents import alerts

    if _WATCH["v"] is not None and time.time() - _WATCH["t"] < 15:
        return _WATCH["v"]
    pf = pfm.build_portfolio()
    if not pf.positions and not pf.derivs:
        return {"time": now_tw().strftime("%H:%M:%S"), "items": [], "trading": alerts.is_trading_hours()}
    codes = list(pf.positions)
    rt = alerts.realtime(codes)
    trading = alerts.is_trading_hours()
    meta = pfm.load_meta()
    ctx = on_demand.context()
    preds = ctx["alpha"]["predictions"]
    base = ctx["alpha"].get("up_base_rate") or 0.5
    hi_p = max(0.6, base + 0.08)
    # 持股建議：優先用盤中建議那一批（每分鐘重算，和警示、總表同一份），沒有才用持股建議快取；不在這裡重算
    plan = {i["code"]: i for i in ((_INTRA.get("v") or {}).get("holding_items")
                                   or (on_demand._PLAN.get("v") or {}).get("items") or [])}
    try:
        rg = live_regime()
    except Exception:  # noqa: BLE001
        rg = None
    try:  # 費半重挫：嚴重 → 高連動股開盤先賣、第 3 天買回；一般 → 續抱不要殺低（研究見 us_shock.py）
        from src.agents import us_shock

        shock = us_shock.assess()
        shock_ex = us_shock.exposure(codes) if shock.get("level") in ("一般重挫", "嚴重") else {}
    except Exception:  # noqa: BLE001
        shock, shock_ex = {}, {}
    items = []
    for c, pos in pf.positions.items():
        q = rt.get(c) or {}
        df = on_demand.price_df(c)
        last_close = float(df.Close.iloc[-1]) if not df.empty else pos.avg_cost
        price = q.get("price") or last_close
        chg = q.get("chg_pct") if q else None
        a = preds.get(c) or {}
        f = a.get("features") or {}
        rsi = (_live_rsi(df.Close, price) if q and trading and len(df) >= 15  # 盤中：日 K 到昨天為止＋即時價
               else f.get("rsi14"))
        ma20 = float(df.Close.tail(20).mean()) if len(df) >= 20 else None
        low10 = float(df.Low.tail(10).min()) if len(df) >= 10 else None
        stop = (meta.get(c) or {}).get("stop")
        p_up, exp_ret = a.get("p_up"), a.get("exp_ret")
        adv = plan.get(c) or {}
        sell, rebound, watch = [], [], []
        if stop and price <= stop:
            sell.append(f"現價 {price:g} 已跌破停損 {stop:g}")
        if q.get("at_limit_down"):
            sell.append("跌停，隔日可能續跌，打開時優先處理")
        if adv.get("verdict") in ("全部出清", "減碼"):
            sell.append(f"持股建議：{adv['verdict']}" + (f"（{adv['reasons'][0]}）" if adv.get("reasons") else ""))
        if chg is not None and chg <= -4 and p_up is not None and p_up < base:
            sell.append(f"今日 {chg:+.2f}% 重挫，模型一個月收正機率 {p_up * 100:.0f}% 低於全市場平均 {base * 100:.0f}%")
        basket = (meta.get(c) or {}).get("basket") or "offense"
        weak_stock = (ma20 and price < ma20) or (p_up is not None and p_up < base)
        sx = shock_ex.get(c) or {}
        if shock.get("level") == "嚴重" and sx.get("linked"):
            sell.append(f"{shock['headline']}：這檔與費半連動 β {sx['beta']}，{shock['first_tw_day'][5:]} 開盤先賣、"
                        f"{shock['rebuy_date'][5:]} 收盤前買回（同樣情況續抱 3 日平均再少約 1.6～1.9%）")
        if rg and rg["state"] == "轉弱" and basket == "offense" and weak_stock:
            sell.append(f"大盤紅燈轉弱，這檔進攻股也走弱（{'跌破 20 日均線' if ma20 and price < ma20 else f'收正機率 {p_up * 100:.0f}% 低於平均'}），提早撤出、改買穩健籃／ETF")
        oversold = []
        if rsi is not None and rsi <= 35:
            oversold.append(f"RSI {rsi:.0f}")
        if low10 and price <= low10 * 1.02:
            oversold.append(f"貼近 10 日低點 {low10:g}")
        if chg is not None and chg <= -3:
            oversold.append(f"今日 {chg:+.2f}%")
        if not sell and shock.get("level") == "一般重挫" and sx.get("linked"):
            rebound.append(f"{shock['headline']}，這檔連動 β {sx['beta']}（估影響 {sx['exp_impact_pct']:+.1f}%）："
                           f"續抱不要殺低，歷史 117 次中位數 2 個交易日回到原價（約 {shock['recover_date'][5:]}）")
        if not sell and not rebound and oversold and p_up is not None and p_up >= hi_p and (exp_ret or 0) > 0.02 and not (stop and price <= stop):
            rebound.append("、".join(oversold) + f"，但模型一個月收正機率 {p_up * 100:.0f}%（全市場平均 {base * 100:.0f}%）、"
                           f"預期 {exp_ret * 100:+.1f}%：續抱、不要殺低")
        if not sell and not rebound:
            if chg is not None and chg <= -3:
                watch.append(f"今日 {chg:+.2f}%")
            if ma20 and price < ma20:
                watch.append(f"跌破 20 日均線 {ma20:.1f}")
            if stop and price <= stop * 1.03:
                watch.append(f"距停損 {stop:g} 不到 3%")
            if watch and rg and rg["level"] == "黃燈" and basket == "offense":
                watch.append(f"大盤黃燈：若加權收盤跌破 {rg['lines']['weak_enter']:,} 點轉紅燈，這檔優先撤出")
        # 只因持股建議「減碼」而列入時，狀態就寫「減碼」（與持股建議、警示用詞一致），不要寫成「盡快賣出」
        only_trim = len(sell) == 1 and adv.get("verdict") == "減碼" and sell[0].startswith("持股建議")
        status = "減碼" if only_trim else "盡快賣出" if sell else "低檔可望反彈" if rebound else "注意" if watch else "正常"
        items.append({"code": c, "name": stock_meta(c)["name"], "status": status, "reasons": sell or rebound or watch,
                      "price": price, "chg_pct": chg, "pnl_pct": round((price / pos.avg_cost - 1) * 100, 2) if pos.avg_cost else 0,
                      "stop": stop, "rsi14": round(rsi, 1) if rsi is not None else None,
                      "p_up": p_up, "exp_ret": exp_ret, "verdict": adv.get("verdict")})
    items += _deriv_watch(pf)
    order = {"盡快賣出": 0, "減碼": 1, "注意": 2, "低檔可望反彈": 3, "正常": 4}
    items.sort(key=lambda x: (order[x["status"]], x["chg_pct"] or 0))
    out = {"time": now_tw().strftime("%H:%M:%S"), "trading": alerts.is_trading_hours(), "base_rate": base, "items": items,
           "counts": {k: sum(1 for i in items if i["status"] == k) for k in order},
           "note": "機率為模型估計（測試期高機率組實際收正約 63%、全部股票約 56%），不是保證；RSI 盤中以即時價計算",
           "regime": {k: rg[k] for k in ("state", "light", "level", "headline", "warnings", "actions", "lines")} if rg else None,
           "us_shock": {k: shock.get(k) for k in ("level", "headline", "advice", "first_tw_day", "rebuy_date", "recover_date")}
           if shock.get("level") in ("一般重挫", "嚴重") else None}
    _WATCH.update(t=time.time(), v=out)
    return out


_QUOTES: dict = {}
INDEX_CHANNELS = {"TAIEX": ("tse_t00.tw", "加權指數"), "OTC": ("otc_o00.tw", "櫃買指數")}


def live_quotes(codes: list[str]) -> dict:
    """即時報價（證交所 MIS，8 秒快取）＋加權、櫃買指數。"""
    import time

    import requests

    from src.agents import alerts

    codes = sorted({c for c in codes if c})[:60]
    key = ",".join(codes)
    hit = _QUOTES.get(key)
    if hit and time.time() - hit[0] < 4:
        return hit[1]
    quotes = alerts.realtime(codes) if codes else {}
    idx = {}
    try:
        r = requests.get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                         params={"ex_ch": "|".join(c for c, _ in INDEX_CHANNELS.values()), "json": 1, "delay": 0}, timeout=10).json()
        names = {ch.split(".")[0].split("_")[1]: (k, n) for k, (ch, n) in INDEX_CHANNELS.items()}
        for m in r.get("msgArray", []):
            k, n = names.get(m.get("c"), (None, None))
            last = float(m.get("z") or m.get("y") or 0)
            prev = float(m.get("y") or 0)
            if k and last and prev:
                idx[k] = {"name": n, "price": round(last, 2), "change": round(last - prev, 2),
                          "chg_pct": round((last / prev - 1) * 100, 2), "time": m.get("t")}
    except Exception:  # noqa: BLE001
        pass
    out = {"quotes": quotes, "indices": idx, "trading": alerts.is_trading_hours()}
    _QUOTES[key] = (time.time(), out)
    return out


_NEWS: dict = {"t": 0.0, "v": None}


def live_news(refresh: bool = False) -> dict:
    """即時新聞：盤中每 2 分鐘、盤後每 5 分鐘自動更新；refresh=True（手動重新整理）時立即抓取，但 30 秒內不重複抓。"""
    import time

    from src.agents import alerts
    from src.agents import news as news_agent
    from src.config import now_tw

    age = time.time() - _NEWS["t"]
    ttl = 120 if alerts.is_trading_hours() else 300
    if _NEWS["v"] is None or age > ttl or (refresh and age > 30):
        items = news_agent.fetch_news(now_tw())
        _NEWS.update(t=time.time(), v={"news": [{k: n[k] for k in ("id", "source", "title", "url", "time", "category")}
                                                for n in items],
                                       "fetched_at": now_tw().strftime("%H:%M:%S")})
    return _NEWS["v"]


_INTL: dict = {}


def intl_impacts(codes: list[str]) -> dict:
    """個股受國際市場影響的註記：歷史敏感度 β × 最近一個交易日的國際因子變動，只列統計顯著且影響 ≥0.4% 者。"""
    import time

    import pandas as pd

    from src.agents import macro
    from src.config import settings

    ctx = on_demand.context()
    target = pd.Timestamp(ctx["target"])
    codes = sorted({c for c in codes if c})[:40]
    key = (ctx["target"], ",".join(codes))
    hit = _INTL.get(key)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    sens = macro.sensitivities(codes, ctx["panel"], ctx["us_hist"], target)
    moves = {}
    for k, cfg in settings()["macro_factors"].items():
        df = ctx["us_hist"].get(cfg["ticker"])
        if df is None:
            continue
        d = df[df.index < target] if cfg["ticker"] not in macro.ASIA else df[df.index <= target]
        if len(d) < 2:
            continue
        last, prev = float(d.Close.iloc[-1]), float(d.Close.iloc[-2])
        moves[k] = {"move": (last - prev) if cfg["mode"] == "diff" else (last / prev - 1), "name": cfg["name"],
                    "date": str(d.index[-1].date()),
                    "label": f"{last - prev:+.2f}" if cfg["mode"] == "diff" else f"{(last / prev - 1) * 100:+.2f}%"}
    out = {}
    for c in codes:
        notes = []
        for k, s in sens.get(c, {}).items():
            if abs(s["corr"]) < macro.MIN_CORR or k not in moves:
                continue
            impact = s["beta"] * moves[k]["move"] * 100
            if abs(impact) >= 0.4:
                region = "日本" if k == "JAPAN" else "中國" if k == "CHINA" else "國際" if k in ("OIL", "GOLD", "USD_TWD") else "美國"
                notes.append({"factor": moves[k]["name"], "region": region, "move": moves[k]["label"], "impact_pct": round(impact, 2),
                              "text": f"{moves[k]['name']} {moves[k]['label']}，依歷史連動估 {impact:+.1f}%"})
        notes.sort(key=lambda n: -abs(n["impact_pct"]))
        if notes:
            out[c] = notes[:2]
    res = {"impacts": out, "factor_date": max((m["date"] for m in moves.values()), default=None)}
    _INTL[key] = (time.time(), res)
    return res


def potential(limit: int = 8) -> dict:
    """潛在標的：模型看好但今天沒有列入下單的股票（候補遞補、過熱等回檔），附「為什麼還沒買」與觸發條件。"""
    r = load_report()
    if not r:
        return {"date": None, "offense": [], "defense": []}
    ordered = {o["code"] for o in r.get("orders", [])}
    if _INTRA["v"] is not None:  # 盤中計畫（每 40 分鐘重算）已列入的也排除
        ordered |= {o["code"] for o in _INTRA["v"].get("orders", [])}
    held = set(pfm.build_portfolio().positions)
    off, dfn = [], []
    for rank, x in enumerate(r.get("top_picks", []), 1):
        if x["code"] in ordered or x["code"] in held:
            continue
        hot = x.get("signal") == "過熱不追"
        why = (f"{x['overheat']}" if hot else
               f"進攻分數 {x['score']:+.2f} 排第 {rank} 名，今天因檔數與資金上限未列入")
        trigger = ("等 RSI 降回 82 以下或回檔整理後重新評估；每天盤前會自動重算" if hot else
                   "前面標的被停損、出清或有新資金時優先遞補；分數維持在門檻以上就可買")
        off.append({"code": x["code"], "name": x["name"], "sector": x.get("sector"), "basket": "offense",
                    "kind": "過熱等回檔" if hot else "候補", "rank": rank, "score": x["score"],
                    "p_newhigh": x.get("p_newhigh"), "exp_excess": x.get("exp_excess"), "dist_hi": x.get("dist_hi"),
                    "rev_yoy": x.get("rev_yoy"), "why_not_yet": why, "trigger": trigger})
    for rank, x in enumerate(r.get("def_picks", []), 1):
        if x["code"] in ordered or x["code"] in held:
            continue
        dfn.append({"code": x["code"], "name": x["name"], "sector": x.get("sector"), "basket": "defense",
                    "kind": "候補", "rank": rank, "score": x["def_score"], "p_up": x.get("p_up"),
                    "exp_dd": x.get("exp_dd"), "beta": x.get("beta"),
                    "why_not_yet": f"穩健分數 {x['def_score']:+.2f} 排第 {rank} 名，今天因檔數與資金上限未列入",
                    "trigger": "穩健籃有持股出清或比例調高時優先遞補"})
    etf_list = []
    try:  # ETF 候補：一個月模型收正機率高、尚未列入下單者（含溢價不追）
        from src.agents import etf as etf_agent

        ctx = on_demand.context()
        ap = ctx["alpha"]["predictions"]
        prem = etf_agent.premiums()
        cfg = params.basket("etf")
        for rank, c in enumerate(sorted([c for c in ap if c in ctx["etf"]], key=lambda c: -ap[c].get("def_combo", 0)), 1):
            if c in ordered or c in held:
                continue
            name = stock_meta(c)["name"]
            pr = (prem.get(c) or {}).get("premium_pct")
            hot = pr is not None and pr > cfg["max_premium_pct"]
            etf_list.append({"code": c, "name": name, "basket": "etf", "kind": "溢價不追" if hot else "候補", "rank": rank,
                             "score": ap[c].get("def_combo"), "p_up": ap[c].get("p_up"), "exp_dd": ap[c].get("exp_dd"),
                             "category": etf_agent.category(c, name), "premium_pct": pr,
                             "why_not_yet": (f"溢價 {pr:+.2f}% 超過 {cfg['max_premium_pct']}%" if hot else
                                             f"ETF 穩健排名第 {rank}，今天 ETF 籃已有更高分或同類別的 ETF"),
                             "trigger": "溢價收斂到 1% 以內再評估" if hot else "ETF 籃有出清或比例調高時遞補（同類別最多 1 檔）"})
            if len(etf_list) >= limit:
                break
    except Exception as e:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning("ETF 潛在標的失敗：%s", e)
    return {"date": r["date"], "target_date": r.get("target_date"), "offense": off[:limit], "defense": dfn[:limit], "etf": etf_list,
            "note": "潛在標的不是今天的下單建議；要買之前請先用個股分析或問 AI 助理取得當下的張數與限價"}


def _fmt_ret(x) -> str:
    return f"{x * 100:+.1f}%" if x is not None else "—"


def decision_board() -> dict:
    """買賣決策總表（使用者要求「直接告訴我哪些能買、哪些不能買、為什麼續抱」）：
    每檔只給一個結論（可以買／等回檔再買／先不要買／續抱／減碼／全部出清），附一句話理由、依據與「什麼情況會改變結論」。
    全部取自盤中建議（每 1 分鐘以即時價重算）與每日排名，不另做判斷。"""
    intra = live_intraday()
    held = set(pfm.build_portfolio().positions)
    if {h["code"] for h in intra.get("holding_items") or []} != held:
        # 舊存檔的持股和現在對不起來（例如成交後還沒重算）：等這次重算完，避免把已持有的股票列成「可以買」
        pf = pfm.build_portfolio()
        intra = _compute_intraday(_live_slot(), (tuple(sorted((c, p.shares) for c, p in pf.positions.items())), round(pf.cash)))
    buy, wait, avoid, hold = [], [], [], []
    seen: set[str] = set()

    # 1) 持股：續抱的理由、減碼／出清的原因
    rec_why = {i["code"]: i.get("why") or "" for i in ((intra.get("recovery") or {}).get("items") or [])}
    for h in intra.get("holding_items") or []:
        m = h.get("model") or {}
        tip = rec_why.get(h["code"], "").partition("提醒：")[2]  # 未滿最短持有天數、但已出現獲利換股機會
        if h["verdict"] == "續抱" and tip:
            hold.append({"code": h["code"], "name": h["name"], "verdict": "續抱", "headline": f"續抱，但留意獲利換股：{tip}",
                         "reasons": (h.get("reasons") or [])[:2], "change_if": (h.get("conditions") or [])[:2],
                         "price": h.get("price"), "pnl_pct": h.get("pnl_pct"), "shares": h.get("shares"), "swap_tip": True})
            seen.add(h["code"])
            continue
        if h["verdict"] == "續抱":
            exp = m.get("exp_ret")
            head = ("續抱：沒有觸發停損或賣出條件" + (f"，模型一個月預期 {_fmt_ret(exp)}" if exp is not None else "")
                    + ("，仍有上漲空間" if (exp or 0) >= 0.03 else "，預期小漲，抱著等停利" if (exp or 0) > 0
                       else "，但模型預期轉弱，盯緊停損"))
        elif h["verdict"] == "加碼":
            head = f"加碼：{h['qty_text']}"
        else:
            head = f"{h['verdict']}：{h['qty_text']}" + (f"，限價 {h['limit_price']}" if h.get("limit_price") else "")
        hold.append({"code": h["code"], "name": h["name"], "verdict": h["verdict"], "headline": head,
                     "reasons": (h.get("reasons") or [])[:3], "change_if": (h.get("conditions") or [])[:2],
                     "price": h.get("price"), "pnl_pct": h.get("pnl_pct"), "shares": h.get("shares")})
        seen.add(h["code"])

    # 2) 沒有持股時的建倉買單（依盤中即時狀態）
    seen |= held  # 已持有的股票只出現在「持股」區，不會又列成可以買
    for o in intra.get("orders") or []:
        if o.get("action") != "BUY" or o["code"] in seen:
            continue
        row = {"code": o["code"], "name": o["name"], "basket": o.get("basket"), "price": o.get("price"),
               "limit_price": o.get("limit_price"), "stop": o.get("stop"), "reasons": [o["why"]] if o.get("why") else [],
               "change_if": [f"收盤跌破停損 {o['stop']} 就出場"] if o.get("stop") else []}
        if o["status"] in ("可以下單", "可預掛單"):
            buy.append({**row, "verdict": "可以買", "headline": f"可以買：{o['advice']}"})
        elif o["status"] in ("接近限價", "不追價", "無報價"):
            wait.append({**row, "verdict": "等回檔再買",
                         "headline": f"等回檔再買：回到 {o['limit_price']} 以下再買（{o['advice']}）"})
        else:
            avoid.append({**row, "verdict": "先不要買", "headline": f"先不要買：{o['advice']}"})
        seen.add(o["code"])

    # 3) 已有持股時的近期推薦：價位到了且資金來源合理才算「可以買」
    for r in intra.get("recommendations") or []:
        if r["code"] in seen:
            continue
        f = r.get("funding") or {}
        lots = f"{r['shares'] // 1000} 張" + (f"又 {r['shares'] % 1000} 股" if r["shares"] % 1000 else "") if r["shares"] >= 1000 else f"{r['shares']} 股"
        row = {"code": r["code"], "name": r["name"], "basket": r.get("basket"), "price": r.get("price"),
               "limit_price": r.get("limit_price"), "stop": r.get("stop"), "exp_ret": r.get("exp_ret"),
               "reasons": (r.get("why") or [])[:2] + [f"風險：{x}" for x in (r.get("risks") or [])[:1]],
               "change_if": [f"收盤跌破停損 {r['stop']} 就出場"] if r.get("stop") else []}
        in_price = r.get("gap_pct") is not None and r["gap_pct"] <= 0
        cash_ok = f.get("covered") and not f.get("sells")
        if not in_price:
            wait.append({**row, "verdict": "等回檔再買",
                         "headline": f"等回檔再買：{r['entry_note']}，回到 {r['limit_price']} 以下再買 {lots}"})
        elif cash_ok:
            buy.append({**row, "verdict": "可以買", "headline": f"可以買：限價 {r['limit_price']} 買 {lots}，現金足夠"})
        elif f.get("worth_all"):
            sells = "、".join(f"賣{s['name']} " + s["note"].split("，")[0].removeprefix("賣 ") for s in f["sells"])
            buy.append({**row, "verdict": "可以買", "headline": f"可以買（要先換股）：先{sells}，再限價 {r['limit_price']} 買 {lots}",
                        "reasons": row["reasons"] + [s["note"] for s in f["sells"]][:2]})
        else:
            why = next((s["note"] for s in f.get("sells", []) if not s["worth"]), "現金不夠")
            avoid.append({**row, "verdict": "先不要買", "headline": "先不要買：現金不夠，賣持股來換不划算",
                          "reasons": row["reasons"] + [why]})
        seen.add(r["code"])

    # 4) 今天不追（盤中大漲、漲停、跌停）與過熱／溢價的候補
    for s in intra.get("skipped") or []:
        if s["code"] not in seen:
            avoid.append({"code": s["code"], "name": s["name"], "verdict": "先不要買", "headline": f"先不要買：{s['reason']}",
                          "reasons": [s["reason"]], "change_if": ["隔天盤前重新評估"]})
            seen.add(s["code"])
    try:
        pot = potential()
    except Exception:  # noqa: BLE001
        pot = {}
    for x in (pot.get("offense") or []) + (pot.get("etf") or []):
        if x["code"] in seen or x["kind"] not in ("過熱等回檔", "溢價不追"):
            continue
        avoid.append({"code": x["code"], "name": x["name"], "verdict": "先不要買", "headline": f"先不要買：{x['kind']}",
                      "reasons": [x["why_not_yet"]], "change_if": [x["trigger"]]})
        seen.add(x["code"])

    order = {"全部出清": 0, "減碼": 1, "加碼": 2, "續抱": 3}
    hold.sort(key=lambda h: order.get(h["verdict"], 9))
    for x in buy + wait + avoid + hold:  # 每檔一行完整說明（程式組好，AI 助理照抄，不自己改寫數字）
        x["line"] = (f"{x['name']}（{x['code']}）{x['headline']}"
                     + (f"。理由：{x['reasons'][0]}" if x.get("reasons") else "")
                     + (f"。{x['change_if'][0]}" if x.get("change_if") else ""))
    sell_n = sum(1 for h in hold if h["verdict"] in ("全部出清", "減碼"))
    summary = [f"可以買 {len(buy)} 檔" + (f"（{'、'.join(b['name'] for b in buy[:4])}）" if buy else ""),
               f"等回檔 {len(wait)} 檔", f"先不要買 {len(avoid)} 檔"]
    if hold:
        summary.append(f"持股續抱 {sum(1 for h in hold if h['verdict'] == '續抱')} 檔" + (f"、要賣 {sell_n} 檔" if sell_n else ""))
    return {"time": intra.get("time"), "stale": bool(intra.get("stale")), "summary": "；".join(summary),
            "buy": buy, "wait": wait, "avoid": avoid[:12], "hold": hold,
            "note": "結論依盤中即時價每 1 分鐘重算；機率與預期報酬是模型估計，不是保證"}


def recovery_plan() -> dict:
    """虧損怎麼補回：持股逐檔比較（即時價，5 分鐘快取）＋期貨補回方案＋ETF 籃建議。"""
    plan = on_demand.portfolio_plan()
    rec = plan.get("recovery") or {}
    pf = portfolio()
    return {"time": plan.get("time"), "nav": pf["nav"], "return_pct": pf["return_pct"],
            "summary": rec.get("summary"),
            "holdings": [{k: i.get(k) for k in ("code", "name", "basket", "unrealized", "pnl_pct", "decision", "why", "lines")}
                         for i in rec.get("items", [])],
            "futures": futures_view(), "etf": etf_view(),
            "note": "期貨的補回虧損方案是放大大盤曝險，不是預測優勢；回測顯示期貨擇時沒有優勢"}


def value_picks(max_price: float = 50, limit: int = 15, include_etf: bool = False) -> dict:
    """平價潛力股：股價 ≤ max_price、模型看漲且波動與回檔小的股票排名（一張的金額低，資金好分配、可買整張）。

    注意：排名比的是報酬率，股價高低本身不影響報酬率；這份清單的好處是用較少資金就能買整張、分散更多檔。
    安全條件（全部要符合）：一個月收正機率 ≥ 全市場平均、預期最大回檔 ≥ -10%、日波動 ≤ 3.5%、β ≤ 1.5、
    RSI ≤ 80（不追過熱）、不在注意／處置股名單。排序：收正機率 45%＋預期報酬 35%＋回檔小 20%（皆為全市場百分位）。
    """
    from src.agents import alerts

    ctx = on_demand.context()
    preds = ctx["alpha"]["predictions"]
    base = ctx["alpha"].get("up_base_rate") or 0.5
    warn = alerts.attention_lists()
    held = set(pfm.build_portfolio().positions)
    rows, dropped = [], {"價格": 0, "安全條件": 0, "注意處置": 0}
    for c, a in preds.items():
        f = a.get("features") or {}
        close = a.get("close")
        is_etf = bool(f.get("is_etf"))
        if not close or close > max_price or (is_etf and not include_etf):
            dropped["價格"] += 1
            continue
        if c in warn:
            dropped["注意處置"] += 1
            continue
        safe = ((a.get("p_up") or 0) >= base and (a.get("exp_dd") or -1) >= -0.10 and (f.get("vol20") or 1) <= 0.035
                and (f.get("beta60") or 9) <= 1.5 and (f.get("rsi14") or 100) <= 80 and (a.get("exp_ret") or 0) > 0)
        if not safe:
            dropped["安全條件"] += 1
            continue
        score = 0.45 * (a.get("up_rank") or 0) + 0.35 * (a.get("alpha_rank") or 0) + 0.20 * (a.get("dd_rank") or 0)
        rows.append({"code": c, "name": stock_meta(c)["name"], "sector": stock_meta(c).get("sector"), "is_etf": is_etf,
                     "price": close, "lot_cost": round(close * 1000), "score": round(score, 3),
                     "p_up": a.get("p_up"), "exp_ret": a.get("exp_ret"), "exp_dd": a.get("exp_dd"), "p_newhigh": a.get("p_newhigh"),
                     "rsi14": round(f.get("rsi14"), 1) if f.get("rsi14") is not None else None,
                     "vol20_pct": round(f["vol20"] * 100, 2) if f.get("vol20") is not None else None,
                     "beta": round(f["beta60"], 2) if f.get("beta60") is not None else None, "held": c in held})
    rows.sort(key=lambda x: -x["score"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return {"asof": ctx["alpha"].get("asof"), "max_price": max_price, "base_rate": base, "items": rows[:limit],
            "n_pass": len(rows), "dropped": dropped,
            "note": "排名看的是報酬率，股價便宜不代表漲得比較多；好處是一張金額低，資金好分配、可以買整張並分散更多檔。"
                    "機率與預期報酬為模型估計（一個月），不是保證；買之前請先看個股分析取得張數與限價。"}


def today_status(code: str, analysis: dict) -> dict:
    """這檔股票在今天建議中的位置：有沒有列入、排第幾、和最後一檔入選的差距、拖累分數的項目。"""
    from src.agents.strategy import COMPONENT_NAMES, DEF_NAMES

    r = load_report() or {}
    orders = r.get("orders", [])
    if _INTRA["v"] is not None:
        orders = orders + [o for o in _INTRA["v"].get("orders", []) if o["code"] not in {x["code"] for x in orders}]
    if any(o["code"] == code for o in orders):
        return {"in_today_orders": True, "note": "已列入今天的下單建議"}
    off_rank = [x["code"] for x in r.get("ranking", [])]
    def_rank = [x["code"] for x in r.get("def_picks", [])]
    off_sel = [o for o in orders if o.get("action") == "BUY" and o.get("basket") == "offense"]
    def_sel = [o for o in orders if o.get("action") == "BUY" and o.get("basket") == "defense"]
    off, dfn = analysis.get("offense") or {}, analysis.get("defense") or {}
    drag = sorted(((v, COMPONENT_NAMES.get(k, k)) for k, v in (off.get("components") or {}).items()), key=lambda t: t[0])[:2]
    ddrag = sorted(((v, DEF_NAMES.get(k, k)) for k, v in (dfn.get("components") or {}).items()), key=lambda t: t[0])[:2]
    return {
        "in_today_orders": False, "report_date": r.get("date"),
        "offense": {"score": off.get("score"), "signal": off.get("signal"),
                    "rank_in_candidates": off_rank.index(code) + 1 if code in off_rank else None,
                    "candidates": len(off_rank), "selected_count": len(off_sel),
                    "lowest_selected_score": min((o["score"] for o in off_sel if o.get("score") is not None), default=None),
                    "weakest_components": [f"{n} {v:+.2f}" for v, n in drag]},
        "defense": {"score": dfn.get("score"), "signal": dfn.get("signal"), "eligible": dfn.get("eligible"),
                    "rank_in_picks": def_rank.index(code) + 1 if code in def_rank else None,
                    "selected_count": len(def_sel),
                    "lowest_selected_score": min((o["score"] for o in def_sel if o.get("score") is not None), default=None),
                    "weakest_components": [f"{n} {v:+.2f}" for v, n in ddrag]},
        "note": "分數元件範圍 -1～+1；每天依綜合分數由高到低取前幾名，分數低於最後一檔入選者就不會列入",
    }


def etf_view() -> dict:
    """ETF 籃：配置比例與理由、候選 ETF（類別、分數、折溢價、建議股數）。"""
    from src.agents import on_demand

    lp = live_intraday()
    a = lp.get("allocation") or {}
    orders = [o for o in lp.get("orders", []) if o.get("basket") == "etf"]
    ctx = on_demand.context()
    from src.agents import etf as etf_agent
    from src.config import stock_meta

    prem = etf_agent.premiums()
    a_preds = ctx["alpha"]["predictions"]
    cands = sorted([c for c in a_preds if c in ctx["etf"]], key=lambda c: -a_preds[c].get("p_up", 0))[:10]
    share = a.get("etf")
    return {"today_etf_share_pct": round(share * 100) if share is not None else None,
            "base_etf_share_pct": round(params.allocation()["etf"]["base"] * 100),
            "note": "回答比例時一律使用 today_etf_share_pct（今日實際比例），base 只是調整前的基準",
            "etf_reasons": a.get("etf_reasons"),
            "planned_orders": [{k: o.get(k) for k in ("code", "name", "planned_shares", "limit_price", "status", "advice")}
                               for o in orders],
            "candidates": [{"code": c, "name": stock_meta(c)["name"],
                            "category": etf_agent.category(c, stock_meta(c)["name"]),
                            "p_up": round(a_preds[c].get("p_up", 0), 3), "exp_dd": round(a_preds[c].get("exp_dd", 0), 4),
                            "premium_pct": (prem.get(c) or {}).get("premium_pct")} for c in cands]}


def futures_view() -> dict:
    lp = live_intraday()
    return {"derivatives": lp.get("derivatives"), "funding": lp.get("funding")}


FUTURES_INFO = {
    "TX": {"name": "臺股期貨（大台）", "aliases": ("台指期", "臺指期", "大台", "臺股期貨", "台股期貨", "TX", "TXF", "台指")},
    "MTX": {"name": "小型臺指（小台）", "aliases": ("小台", "小型台指", "小型臺指", "MTX", "MXF")},
    "TMF": {"name": "微型臺指（微台）", "aliases": ("微台", "微型台指", "微型臺指", "TMF")},
    "TXO": {"name": "臺指選擇權", "aliases": ("選擇權", "台指選", "臺指選", "TXO", "賣權", "買權", "PUT", "CALL")},
}


def futures_search(q: str) -> list[dict]:
    q = q.strip().upper()
    out = []
    for root, info in FUTURES_INFO.items():
        if q and any(q in a.upper() or a.upper() in q for a in info["aliases"]):
            out.append({"code": root, "name": info["name"], "sector": "期貨" if root != "TXO" else "選擇權",
                        "kind": "futures"})
    return out


def _ma(vals: list[float], n: int, i: int):
    return round(sum(vals[i - n + 1:i + 1]) / n, 1) if i >= n - 1 else None


def futures_kline(root: str, days: int = 260) -> dict:
    """期貨近月連續日 K（期交所歷史行情；選擇權用台指期近月代表標的走勢）＋建議價位線＋成交紀錄。"""
    from src.agents import futures_data

    base = "TX" if root == "TXO" else root
    hist = futures_data.history(base, max(days, 70))
    closes = [h["close"] for h in hist]
    candles = [{**{k: h[k] for k in ("time", "open", "high", "low", "close")}, "volume": int(h["volume"]),
                "ma5": _ma(closes, 5, i), "ma20": _ma(closes, 20, i), "ma60": _ma(closes, 60, i), "inst": None}
               for i, h in enumerate(hist)][-days:]
    trades = [t for t in pfm.load_trades() if str(t.get("code", "")).upper() == root]
    markers = [{"time": t["date"], "side": t["side"], "shares": int(float(t["shares"])), "price": float(t["price"])} for t in trades]
    lines = []
    d = (_INTRA["v"] or {}).get("derivatives") or {}
    for o in d.get("orders", []):
        if o["code"] == base or root in ("TX", o["code"]):
            lines += [{"label": f"建議進場 {o['contract']}", "price": o["limit_price"], "kind": "entry"},
                      {"label": "停損", "price": o["stop"], "kind": "stop"},
                      {"label": "停利", "price": o["take_profit"], "kind": "tp"}]
    if root == "TXO" and d.get("hedge"):
        lines.append({"label": f"賣權履約價 {int(d['hedge']['strike'])}（開始有保護）", "price": d["hedge"]["strike"], "kind": "stop"})
    return {"code": root, "name": FUTURES_INFO.get(root, {}).get("name", root), "candles": candles, "markers": markers,
            "lines": lines, "source": "期交所期貨每日交易行情（近月合約連續；每日取成交量最大的月合約）"
            + ("；選擇權以台指期近月代表標的走勢" if root == "TXO" else "")}


def futures_detail(root: str) -> dict:
    from src.agents import futures_data as fd
    from src.config import rules as _rules

    root = root.upper()
    spec = _rules()["futures"]["contracts"].get(root, {})
    margins = fd.margins()
    out = {"root": root, "name": FUTURES_INFO.get(root, {}).get("name", root), "multiplier": spec.get("multiplier"),
           "tax": "股價類期貨交易稅 0.002%" if root != "TXO" else "選擇權交易稅 0.01%（競賽規則）",
           "commission_assumed": _rules()["futures"]["commission_per_contract"].get(root),
           "trading_hours": "08:45～13:45（競賽平台接受前一日 15:30 後預掛）"}
    if root == "TXO":
        months = [fd.front_month(), fd.front_month(min_days=40)]
        spot = fd.futures_quotes("TX").get("spot")
        chains = {}
        for ym in dict.fromkeys(months):
            ch = fd.put_chain(ym)
            if spot:
                ch = [r for r in ch if spot * 0.88 <= r["strike"] <= spot * 1.02]
            chains[ym] = [{**r, "dist_pct": round((r["strike"] / spot - 1) * 100, 2) if spot else None,
                           "premium_per_lot": round(r["price"] * 50) if r.get("price") else None} for r in ch]
        out.update(spot=spot, months=[{"ym": ym, "settlement": str(fd.settlement_date(ym)),
                                       "days_left": fd.days_to_settlement(ym)} for ym in chains],
                   puts=chains, note="只做買方：買進賣權的最大損失＝權利金；價外越多越便宜、保護越晚開始")
    else:
        q = fd.futures_quotes(root)
        rows = []
        for ym, m in sorted(q["months"].items())[:4]:
            rows.append({"ym": ym, "contract": f"{root}{ym}", **m, "settlement": str(fd.settlement_date(ym)),
                         "days_left": fd.days_to_settlement(ym),
                         "basis": round(m["price"] - q["spot"], 1) if q.get("spot") and m.get("price") else None})
        mg = margins.get(root, {})
        front = rows[0] if rows else {}
        out.update(spot=q.get("spot"), source=q.get("source"), months=rows, margin=mg,
                   notional=round(front["price"] * spec["multiplier"]) if front.get("price") else None,
                   leverage=round(front["price"] * spec["multiplier"] / mg["initial"], 1) if front.get("price") and mg.get("initial") else None)
    plan = (_INTRA["v"] or {}).get("derivatives")
    out["system_view"] = plan
    return out


def weekly_report(week: str | None = None) -> dict:
    """產生指定一週（任一天即可）的週報 PPT，存到 reports/weekly/，回傳下載路徑。"""
    import datetime as dt

    from src.agents import weekly
    from src.config import REPORT_DIR, now_tw
    from src.reports import weekly_pptx

    day = dt.date.fromisoformat(week) if week else now_tw().date()
    w = weekly.collect(day)
    text, verified = weekly.narrate(w, weekly.next_week_plan())
    name = f"{w['week_label'].replace(' ', '')}週報_{w['monday']}.pptx"
    path = weekly_pptx.build(w, text, verified, REPORT_DIR / "weekly" / name)
    from pptx import Presentation

    return {"file": f"weekly/{name}", "name": name, "week_no": w["week_no"], "week_label": w["week_label"], "period": f"{w['monday']}～{w['end_day']}",
            "slides": len(Presentation(str(path)).slides), "trades": w["trade_count_week"], "verified_text": verified,
            "headline": text.headline}


def weekly_reports() -> list[dict]:
    from src.config import REPORT_DIR

    out = []
    for fp in sorted((REPORT_DIR / "weekly").glob("*.pptx"), key=lambda p: p.stat().st_mtime, reverse=True):
        import datetime as dt

        out.append({"file": f"weekly/{fp.name}", "name": fp.name,
                    "generated_at": dt.datetime.fromtimestamp(fp.stat().st_mtime).strftime("%m/%d %H:%M")})
    return out


def competition_weeks() -> list[dict]:
    """比賽每一週的週一～週五（給前端選週）。"""
    import datetime as dt

    from src.config import rules as _rules

    c = _rules()["competition"]
    start, end = dt.date.fromisoformat(str(c["start"])), dt.date.fromisoformat(str(c["end"]))
    mon = start - dt.timedelta(days=start.weekday())
    out, n = [], 1
    while mon <= end:
        out.append({"week_no": n, "monday": mon.isoformat(), "friday": (mon + dt.timedelta(days=4)).isoformat()})
        mon += dt.timedelta(days=7)
        n += 1
    return out


# ---------------------------------------------------------------- 系統健康
_HEALTH: dict = {"t": 0.0, "runs": None}
WORKFLOWS = {"invest-agent.yml": "每日排程（盤前／盤中／盤後／週報）", "keepalive.yml": "網站保持清醒", "deploy-space.yml": "網站部署"}


def _gh() -> tuple[str, dict] | None:
    import os

    repo = os.getenv("GITHUB_REPO") or "ZHANGFU-XIANG/xq"
    tok = os.getenv("GITHUB_TOKEN") or os.getenv("GH_PAT")
    if not tok:
        return None
    return repo, {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}


def _workflow_runs() -> dict:
    """各 workflow 最近的執行（5 分鐘快取）。"""
    import time

    import requests

    if _HEALTH["runs"] is not None and time.time() - _HEALTH["t"] < 300:
        return _HEALTH["runs"]
    out = {}
    gh = _gh()
    if gh:
        repo, h = gh
        for wf, label in WORKFLOWS.items():
            try:
                runs = requests.get(f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/runs?per_page=15",
                                    headers=h, timeout=20).json().get("workflow_runs", [])
            except Exception as e:  # noqa: BLE001
                out[wf] = {"label": label, "error": str(e)[:120], "runs": []}
                continue
            out[wf] = {"label": label, "runs": [{"time": r["created_at"], "event": r["event"], "status": r["status"],
                                                  "conclusion": r["conclusion"], "url": r["html_url"]} for r in runs]}
    _HEALTH.update(t=time.time(), runs=out)
    return out


def health() -> dict:
    """排程是否照時間執行、資料是否新鮮；異常會列出原因與處理方式。"""
    import datetime as dt

    from src import config
    from src.agents import intraday
    from src.api import sync
    from src.data import providers

    now = config.now_tw()
    today = now.date()
    trading = config.is_trading_day(today)
    hm = now.hour * 60 + now.minute
    checks = []

    def add(name: str, status: str, detail: str, fix: str = "") -> None:
        checks.append({"name": name, "status": status, "detail": detail, "fix": fix})

    def tw(iso: str) -> dt.datetime:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(config.TW_TZ)

    runs = _workflow_runs()
    for wf, info in runs.items():
        # 排程重複觸發時 GitHub 會取消排隊中的那一個，屬正常，不算失敗
        rs = [r for r in info.get("runs") or [] if r["conclusion"] != "cancelled"]
        if info.get("error"):
            add(info["label"], "unknown", f"無法讀取 GitHub：{info['error']}")
            continue
        if not rs:
            add(info["label"], "warn" if wf != "invest-agent.yml" else "unknown", "還沒有執行紀錄")
            continue
        last = rs[0]
        ok = next((r for r in rs if r["conclusion"] == "success"), None)
        last_t = tw(last["time"]).strftime("%m/%d %H:%M")
        if last["status"] != "completed":
            add(info["label"], "running", f"執行中（{last_t} 開始）")
        elif last["conclusion"] == "success":
            add(info["label"], "ok", f"最近一次 {last_t} 成功")
        else:
            add(info["label"], "error", f"最近一次 {last_t} {last['conclusion']}" +
                (f"；上次成功 {tw(ok['time']).strftime('%m/%d %H:%M')}" if ok else "；尚無成功紀錄"),
                "到 GitHub Actions 看錯誤訊息，或請 AI 助理重新執行")
    if "keepalive.yml" in runs:
        rs = [r for r in runs["keepalive.yml"].get("runs", []) if r["event"] == "schedule"]
        if not rs:
            add("保持清醒排程", "warn", "每 6 小時的排程還沒有自動觸發過（GitHub 新排程有時會延遲幾小時）")

    # 資料新鮮度
    latest = load_report() or {}
    if trading and hm >= 7 * 60 + 40:
        if latest.get("target_date") == today.isoformat():
            add("今天的盤前報告", "ok", f"{latest.get('generated_at')} 產生")
        else:
            add("今天的盤前報告", "error", f"還沒有 {today} 的盤前報告（07:00 應產生）", "請 AI 助理「重新執行盤前報告」")
    else:
        add("最新報告", "ok" if latest else "warn", f"{latest.get('date', '無')} {latest.get('title', '')}（{latest.get('generated_at', '—')}）")
    files = sorted((config.DATA_DIR / "intraday").glob("*.json"))
    if files:
        f = files[-1].stem  # 20261005_0940
        last = dt.datetime.strptime(f, "%Y%m%d_%H%M").replace(tzinfo=config.TW_TZ)
        age = (now - last).total_seconds() / 60
        stale = trading and 9 * 60 + 10 <= hm <= 13 * 60 + 40 and age > 70
        in_session = trading and 9 * 60 <= hm <= 13 * 60 + 40
        idle = "" if in_session else "（盤中 09:00～13:00 每 40 分鐘一輪，收盤後與休市日不更新，屬正常）"
        add("雲端盤中建議", "error" if stale else "ok", f"最近一次 {last.strftime('%m/%d %H:%M')}（{age:.0f} 分鐘前）{idle}",
            "盤中每 40 分鐘一輪，超過 70 分鐘沒更新代表排程延遲或失敗" if stale else "")
    elif trading and hm >= 9 * 60 + 10:
        add("雲端盤中建議", "warn", "今天還沒有雲端盤中建議紀錄")
    if sync.cloud():
        from src.api import scheduler

        sc = scheduler.state
        if sc.get("error"):
            add("準點觸發排程", "error", f"觸發失敗：{sc['error']}", "檢查 Space 的 GITHUB_TOKEN 是否有 Actions 寫入權限；cron 備援仍會執行（可能延遲）")
        else:
            add("準點觸發排程", "ok", f"最近一次 {sc['last']['time']} 觸發 {sc['last']['key']}" if sc.get("last") else "今天尚未到排程時間")
        from src.api import notifier

        ns = notifier.state
        add("手機即時通知（Email）", "warn" if ns.get("error") else "ok",
            ns.get("error") or (f"今天已寄 {ns['count']} 封，最近：{ns['last']}" if ns.get("last") else "盤中有狀況才會寄"))
        st = sync.state
        ls = st.get("last_sync")
        ls = dt.datetime.fromtimestamp(ls, config.TW_TZ).strftime("%m/%d %H:%M") if isinstance(ls, (int, float)) else (ls or "尚未")
        add("與 GitHub 同步", "error" if st.get("error") else "ok", f"最近一次 {ls}" +
            (f"；錯誤：{st['error']}" if st.get("error") else ""))
    ps = providers.status()
    add("FinMind 資料來源", "ok" if ps["finmind_available"] else "warn",
        f"{ps['finmind_tokens']} 組 token" + ("；全部冷卻中，暫用官方資料" if not ps["finmind_available"] else ""))
    order = {"error": 0, "warn": 1, "unknown": 2, "running": 3, "ok": 4}
    overall = min((c["status"] for c in checks), key=lambda s: order.get(s, 9), default="ok")
    return {"time": now.strftime("%Y-%m-%d %H:%M"), "overall": overall, "checks": checks,
            "slot": intraday.current_slot(now),
            "runs": {wf: {"label": v["label"], "runs": v.get("runs", [])[:8]} for wf, v in runs.items()}}


_PICKS: dict = {}


def _atr14(df: pd.DataFrame, before=None) -> float | None:
    d = df[df.index < pd.Timestamp(before)].tail(15) if before is not None else df.tail(15)
    if len(d) < 15:
        return None
    tr = pd.concat([d.High - d.Low, (d.High - d.Close.shift()).abs(), (d.Low - d.Close.shift()).abs()], axis=1).max(axis=1)
    return float(tr.tail(14).mean())


def add_trading_days(d, n: int):
    """d 之後第 n 個交易日。"""
    import datetime as dt

    from src.config import is_trading_day

    cur = d
    while n > 0:
        cur += dt.timedelta(days=1)
        if is_trading_day(cur):
            n -= 1
    return cur


def trading_days_between(a, b) -> int:
    """a（不含）到 b（含）之間的交易日數。"""
    import datetime as dt

    from src.config import is_trading_day

    n, cur = 0, a
    while cur < b:
        cur += dt.timedelta(days=1)
        n += is_trading_day(cur)
    return n


EVENT_EXCESS = {"A": 6.4, "L": 2.6, "B": 2.8}  # 資訊事件 20 日超額報酬（%，events.py 研究；漲停用隔天開盤買的數字）


def _news_credibility(news: list[dict]) -> tuple[float, str]:
    """消息可信度（防假消息、傳聞）：公開資訊觀測站正式公告或 2 個以上不同來源 → 1.0；
    只有 1 個來源 → 0.5；查無相關消息 → 0.3；公司發「澄清」→ 0.3。規則化判斷，無法回測（沒有歷史新聞資料）。"""
    titles = [n.get("title") or "" for n in news]
    if any("澄清" in t for t in titles):
        return 0.3, "公司發布澄清公告，消息可能是傳聞或媒體誤報"
    official = [n for n in news if n.get("source") == "公開資訊觀測站"]
    sources = {n.get("source") for n in news if n.get("source")}
    if official:
        return 1.0, f"公開資訊觀測站有正式公告（{official[0]['title'][:24]}）"
    if len(sources) >= 2:
        return 1.0, f"{len(sources)} 個不同新聞來源報導"
    if len(sources) == 1:
        return 0.5, "只有 1 個來源報導，未經公司正式公告，打折計算"
    return 0.3, "查不到相關新聞或公告，原因不明（可能是傳聞或主力拉抬），打折計算"


def high_return_picks(limit: int = 20, include_etf: bool = False, budget: float | None = None, split: int = 1,
                      odd: bool = False, max_price: float | None = None) -> dict:
    """綜合排名（總覽用）：把各種已回測的獲利因素換算成「預期 20 日報酬」相加後排序，不參考本隊持股與資金。

    組成（每項都有各自的回測，見各模組；相加是假設，組合本身未另外回測）：
    1. 一個月模型預期報酬（10 年資料；樣本外前 20 名 20 日 +6.7%、全市場 +3.0%）
    2. 資訊事件：今天爆量跳空大漲（events.py：買得到的資訊衝擊超額 +6.4%、漲停隔天開盤買 +2.6%、強勢放量 +2.8%）
       × 消息可信度（_news_credibility：正式公告／多來源 1.0、單一來源 0.5、查無消息或公司澄清 0.3）
    3. 美股嚴重重挫且與費半高度連動：−1.8%（us_shock.py：之後 3 天平均再跌 1.6～1.9%）並改成等回檔再買
    排除：走勢鋸齒／成交值偏低（10 日報酬 +3.80% → +4.34%）。
    大盤燈號只顯示不扣分（模型推薦股在紅燈期 40 筆 20 日 +12.4%、強勢期 740 筆 +7.5%，沒有一致關係）。
    max_price：股價上限（元／股）；budget＋split：每檔預算（AI 用）。勝率＝模型估計 20 日收正機率（由 10 年歷史相似情境學得）。"""
    import time

    from src.agents import alerts, alpha_model, events, sizer, us_shock
    from src.config import settings

    key = (limit, include_etf, budget, split, odd, max_price)
    hit = _PICKS.get(key)
    if hit and time.time() - hit[0] < (20 if alerts.is_trading_hours() else 300):
        return hit[1]
    pc = settings()["picks"]
    ctx = on_demand.context()
    al = ctx["alpha"]
    preds = al["predictions"]
    warn = alerts.attention_lists()
    pool = [c for c, a in preds.items() if include_etf or not (a.get("features") or {}).get("is_etf")]
    pool.sort(key=lambda c: -(preds[c].get("combo") or 0))
    rank_of = {c: i for i, c in enumerate(pool, 1)}
    per = budget / max(1, split) if budget else None
    cap = max_price or (per / (1 if odd else 1000) if per else None)
    if cap:  # 先用收盤價粗篩（留 10% 給盤中漲幅），再用即時價確認
        pool = [c for c in pool if (preds[c].get("close") or 0) <= cap * 1.1]
    top = pool[:max(80, limit * 4)]
    # 排除「漲一天就回去」的鋸齒股（例：艾笛森）：候選中走勢效率比或 20 日均成交值最低 1/3 不列入
    er = {}
    for c in top:
        df_ = ctx["panel"].get(c)
        if df_ is None or len(df_) < 22:
            continue
        cl = df_[df_.index < pd.Timestamp(ctx["target"])].Close.tail(21)
        moves = cl.pct_change().abs().sum()
        er[c] = abs(cl.iloc[-1] / cl.iloc[0] - 1) / moves if moves else 0
    er_cut = pd.Series(list(er.values())).quantile(1 / 3) if er else 0
    val_cut = pd.Series([(preds[c].get("features") or {}).get("log_val20") or 0 for c in top]).quantile(1 / 3) if top else 0
    choppy = {c for c in top if c in er and (er[c] < er_cut or ((preds[c].get("features") or {}).get("log_val20") or 0) < val_cut)}
    # 資訊事件（全市場掃描，事件股不受鋸齒排除：它今天就是因消息放量）
    try:
        ev = {e["code"]: e for e in events.scan(limit=40)["items"] if e["code"] in preds}
    except Exception as e_:  # noqa: BLE001
        log_ = __import__("logging").getLogger(__name__)
        log_.warning("資訊事件掃描失敗：%s", e_)
        ev = {}
    cand = list(dict.fromkeys([c for c in top if c not in choppy] + [c for c in ev if not cap or ev[c]["price"] <= cap]))
    rt = alerts.realtime(cand)
    try:
        shock = us_shock.assess()
        shock_ex = us_shock.exposure(cand) if shock.get("level") == "嚴重" else {}
    except Exception:  # noqa: BLE001
        shock, shock_ex = {}, {}
    try:
        rg = live_regime()
    except Exception:  # noqa: BLE001
        rg = None
    entry_day = now_tw().date() if alerts.is_trading_hours() else on_demand.current_target()
    exit_day, review_day = add_trading_days(entry_day, pc["hold_days"]), add_trading_days(entry_day, pc["review_days"])
    rows = []
    for c in cand:
        a = preds[c]
        f = a.get("features") or {}
        q = rt.get(c) or {}
        price = q.get("price") or a.get("close")
        if cap and price > cap:
            continue
        etf = bool(f.get("is_etf"))
        shares = None
        if per:
            shares = int(per // price) if odd else int(per // (price * 1000)) * 1000
            if shares <= 0:
                continue
        # ---- 綜合預期報酬（%）
        model_e = (a.get("exp_ret") or 0) * 100
        parts = [{"name": "一個月模型", "pct": round(model_e, 2), "note": f"排名第 {rank_of.get(c, '—')}、創新高機率 {(a.get('p_newhigh') or 0) * 100:.0f}%"}]
        e = ev.get(c)
        cred, cred_note = None, None
        if e:
            cred, cred_note = _news_credibility(e.get("news") or [])
            bonus = EVENT_EXCESS[e["tier"]] * cred
            parts.append({"name": f"資訊事件・{e['label']}", "pct": round(bonus, 2),
                          "note": f"歷史超額 +{EVENT_EXCESS[e['tier']]}% × 可信度 {cred}（{cred_note}）"})
        sx = shock_ex.get(c) or {}
        if shock.get("level") == "嚴重" and sx.get("linked"):
            parts.append({"name": "美股嚴重重挫", "pct": -1.8, "note": f"與費半連動 β {sx['beta']}，之後 3 天平均再跌 1.6～1.9%"})
        score = round(sum(p_["pct"] for p_ in parts), 2)
        p_up = a.get("p_up") or 0
        conf = ("低" if (cred is not None and cred < 0.5) or p_up < 0.5 else
                "高" if p_up >= 0.6 and (cred is None or cred >= 1.0) else "中")
        df = ctx["panel"].get(c)
        atr = _atr14(df, ctx["target"]) if df is not None else None
        stop = sizer.round_tick(price - pc["stop_atr"] * atr, etf) if atr else None
        target = sizer.round_tick(price * (1 + max(score, 0) / 100), etf, up=True)
        if (warn.get(c) or {}).get("type") == "處置":
            verdict, conf = "先不要買", "低"
            entry = ("處置股（分盤撮合約 5～20 分鐘一次、大量委託需預收款）：十年回測推薦名單中處置中的股票 40 日 +6.3%、"
                     "非處置 +8.3%，投資組合排除處置股年化 59% → 65%")
        elif q.get("at_limit_up"):
            verdict = "等回檔再買"
            entry = f"今天已漲停（{price}），排隊買；沒買到就下一個交易日開盤直接買"
        elif sx.get("linked") and shock.get("level") == "嚴重":
            verdict = "等回檔再買"
            entry = f"{shock['headline']}，與費半高度連動：{shock['rebuy_date'][5:]} 收盤前再買"
        elif score <= 0:
            verdict = "先不要買"
            entry = "綜合預期報酬不是正的"
        else:
            verdict = "可以買"
            entry = (f"{'收盤前' if e and alerts.is_trading_hours() else '現在'}直接買，限價 {sizer.round_tick(price, etf, up=True)}"
                     "（回測：直接買 20 日 +8.6%，等回檔掛低價常買不到最強的股票，只剩 +2.5～6.0%）")
        rows.append({
            "code": c, "name": stock_meta(c)["name"], "sector": stock_meta(c).get("sector"), "is_etf": etf,
            "score": score, "parts": parts, "confidence": conf, "credibility": cred, "credibility_note": cred_note,
            "model_rank": rank_of.get(c), "event": ({k: e[k] for k in ("tier", "label", "gap_pct", "volume_ratio")}
                                                    | {"news": [n["title"] for n in (e.get("news") or [])[:3]]}) if e else None,
            "verdict": verdict, "entry_note": entry, "price": price, "chg_pct": q.get("chg_pct"),
            "exp_ret": a.get("exp_ret"), "p_newhigh": a.get("p_newhigh"), "p_up": a.get("p_up"), "exp_dd": a.get("exp_dd"),
            "exp_ret_long": a.get("exp_ret_long"), "rsi14": round(f["rsi14"], 1) if f.get("rsi14") is not None else None,
            "stop": stop, "target": target, "lot_cost": round(price * 1000),
            "shares": shares, "qty_text": sizer.fmt_shares(shares) if shares else None,
            "amount": round(shares * price) if shares else None,
            "exit": {"date": exit_day.isoformat(), "review": review_day.isoformat(),
                     "text": f"抱到 {exit_day:%m/%d}（第 {pc['hold_days']} 個交易日）出場，{review_day:%m/%d} 檢查一次；"
                             f"收盤跌破「進場後最高收盤 − {pc['trail_atr']}×ATR」"
                             + (f"（目前約 {round(pc['trail_atr'] * atr, 1)} 元）" if atr else "") + "可鎖利出場"},
            "attention": (warn.get(c) or {}).get("type"),
            "reasons": alpha_model.explain(a),
        })
    order = {"可以買": 0, "等回檔再買": 1, "先不要買": 2}
    rows.sort(key=lambda r: (order[r["verdict"]], -r["score"]))
    rows = rows[:limit]
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    m = al.get("metrics") or {}
    out = {"asof": al.get("asof"), "time": now_tw().strftime("%Y-%m-%d %H:%M:%S"),
           "trading": alerts.is_trading_hours(), "n_universe": len(preds), "items": rows, "n_choppy": len(choppy),
           "n_events": len(ev), "max_price": cap,
           "budget": budget, "split": split, "per_stock": round(per) if per else None, "odd": odd,
           "regime": {k: rg.get(k) for k in ("light", "headline")} if rg else None,
           "us_shock": {k: shock.get(k) for k in ("level", "headline", "advice")} if shock.get("level") in ("一般重挫", "嚴重") else None,
           "backtest": {"topk": params.alpha()["top_k"], "topk_ret": m.get("topk_ret"), "mkt_ret": m.get("mkt_ret"),
                        "topk_newhigh": m.get("topk_newhigh"), "base_rate": m.get("base_rate"),
                        "off_up_rate": m.get("off_up_rate"), "off_dd": m.get("off_dd"), "auc": m.get("auc"),
                        "folds": [{k: f_.get(k) for k in ("start", "end", "topk_ret", "mkt_ret")} for f_ in m.get("folds", [])]},
           "entry_exit_backtest": [
               "進場（每期前 20 名、持有 20 日、扣成本）：隔日開盤直接買 +8.6%、掛昨收 +6.0%、昨收−0.5ATR +4.4%、"
               "回測 5 日線 +4.1%、回測 10 日線 +2.5%",
               "出場（直接買進後）：抱 40 日 +17.4%、抱 20 日 +8.6%、移動停利 4ATR +11.0%、排名掉出前 200 名 +9.7%、"
               "跌破 20 日線 +6.8%、固定停利 15%／停損 8% +1.5%"],
           "method": ("綜合預期報酬＝一個月模型預期報酬＋資訊事件歷史超額報酬×消息可信度－美股嚴重重挫影響，依此排序；"
                      "消息可信度：公開資訊觀測站正式公告或 2 個以上來源算 1、單一來源 0.5、查無消息或公司澄清 0.3（防假消息與傳聞）。"
                      "排除走勢鋸齒與成交值偏低的股票；勝率是模型由 10 年歷史相似情境估計的 20 日收正機率。"
                      "各組成都有回測，相加後的組合未另外回測；大盤燈號只顯示不扣分。")}
    _PICKS[key] = (time.time(), out)
    return out


def exit_plan(code: str, entry_date: str | None = None, entry_price: float | None = None) -> dict:
    """個股退場時間與條件。持有中用實際最早買進日與平均成本；沒持有則以今天買進試算；也可指定買進日與價格。
    規則（settings.picks，回測見 high_return_picks）：第 hold_days 個交易日出場、review_days 檢查、
    移動停利線只當參考。2026-10-12 十年回測（2021-11～2026-10，含 2022 空頭）：
    大盤紅燈全部出場年化 59% → 33%、2022 年 +6% → −8.5%（拿掉）；移動停利 4ATR 在綠／灰／紅燈都輸抱 40 日（只當參考）。"""
    import datetime as dt

    from src.agents import alerts, market_data
    from src.config import settings

    pc = settings()["picks"]
    code = code.strip().upper()
    ctx = on_demand.context()
    df = market_data.tw_price(code)
    if df is None or df.empty:
        return {"code": code, "error": "查無行情"}
    q = alerts.realtime([code]).get(code) or {}
    price = float(q.get("price") or df.Close.iloc[-1])
    today = now_tw().date()
    held = pfm.build_portfolio().positions.get(code)
    source = "指定買進日"
    if entry_date is None and held is not None and held.lots:
        entry_date, source = str(held.lots[0].date), "目前持股（最早一筆仍持有的買進日、平均成本）"
        entry_price = entry_price or round(held.avg_cost, 2)
    if entry_date is None:
        entry_date, source = today.isoformat(), "今天買進試算"
    ed = dt.date.fromisoformat(str(entry_date)[:10])
    entry_price = float(entry_price or price)
    since = df[df.index >= pd.Timestamp(ed)]
    hi = max([entry_price, price] + ([float(since.Close.max())] if len(since) else []))
    atr = _atr14(df)
    trail = round(hi - pc["trail_atr"] * atr, 2) if atr else None
    held_days = trading_days_between(ed, today)
    exit_day, review_day = add_trading_days(ed, pc["hold_days"]), add_trading_days(ed, pc["review_days"])
    rg = None
    try:
        rg = live_regime()
    except Exception:  # noqa: BLE001
        pass
    shock, sx = {}, {}
    try:
        from src.agents import us_shock

        shock = us_shock.assess()
        sx = us_shock.exposure([code]).get(code) or {}
    except Exception:  # noqa: BLE001
        pass
    if shock.get("level") == "嚴重" and sx.get("linked"):
        verdict, why = "先出場、之後買回", (f"{shock['headline']}，這檔與費半連動 β {sx['beta']}：{shock['advice']}")
    elif held_days >= pc["hold_days"]:
        verdict, why = "出場", f"已持有 {held_days} 個交易日，達到 {pc['hold_days']} 日退場點"
    else:
        verdict = "續抱"
        why = (f"已持有 {held_days} 個交易日，還有 {pc['hold_days'] - held_days} 個交易日到退場日 {exit_day:%m/%d}"
               + (f"；{review_day:%m/%d} 是中途檢查日" if held_days < pc["review_days"] else "")
               + "；期間若出現預期更好的股票，持股建議會提示結單轉股")
        if trail and price < trail and held_days > 0:
            why += (f"。現價已跌破移動停利線 {trail}：想先鎖利可以賣，但回測顯示照移動停利出場平均比抱滿少賺"
                    "（十年回測各種大盤狀態都一樣）")
    a = ctx["alpha"]["predictions"].get(code) or {}
    return {"code": code, "name": stock_meta(code)["name"], "source": source, "entry_date": ed.isoformat(),
            "entry_price": entry_price, "price": price, "pnl_pct": round((price / entry_price - 1) * 100, 2),
            "held_days": held_days, "verdict": verdict, "why": why,
            "exit_date": exit_day.isoformat(), "review_date": review_day.isoformat(),
            "highest_close": round(hi, 2), "atr14": round(atr, 2) if atr else None, "trail_stop": trail,
            "regime": rg.get("headline") if rg else None,
            "us_shock": ({"level": shock.get("level"), "headline": shock.get("headline"), "advice": shock.get("advice"),
                          "beta_sox": sx.get("beta"), "linked": sx.get("linked")} if shock.get("level") in ("一般重挫", "嚴重") else None),
            "model_now": {"exp_ret": a.get("exp_ret"), "p_newhigh": a.get("p_newhigh"), "p_up": a.get("p_up")} if a else None,
            "rules": [f"時間出場：進場後第 {pc['hold_days']} 個交易日（回測抱 40 日平均 +17.4%，優於各種停利法）",
                      f"中途檢查：第 {pc['review_days']} 個交易日",
                      f"鎖利參考：進場後最高收盤 − {pc['trail_atr']}×ATR（回測照它出場平均比抱滿少賺，只當參考）",
                      "結單轉股：出現扣成本後預期明顯更好的股票時全部賣出換股（見持股建議）"],
            "note": "十年回測（2021～2026，含 2022 空頭）；機率與報酬為模型估計，不是保證"}
