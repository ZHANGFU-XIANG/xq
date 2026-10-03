"""從 XQ 全球贏家匯入持股：使用者在 XQ「帳務／庫存」畫面全選複製（Ctrl+A、Ctrl+C），貼到網站或 AI 助理。

解析：依表頭關鍵字找欄位（代碼、名稱、庫存股數／張數、成本均價、類別），支援 Tab 或多個空白分隔、千分位。
對帳：與系統紀錄（data/trades.csv）比較，產生「調整成交」建議；使用者確認後才寫入。
XQ 的帳務資料在本機是加密檔，系統不讀取、也不破解；只解析使用者主動貼上的內容。
"""
from __future__ import annotations

import re

from src.config import now_tw, stock_meta
from src.state import portfolio as pfm

COLS = {
    "code": ["商品代碼", "股票代號", "代碼", "代號", "商品代號", "證券代號"],
    "name": ["商品名稱", "股票名稱", "商品", "名稱", "證券名稱"],
    "shares": ["庫存股數", "現股庫存", "持有股數", "庫存數量", "今日庫存", "股數", "庫存", "數量"],
    "lots": ["庫存張數", "持有張數", "張數"],
    "cost": ["成本均價", "持有均價", "平均成本", "成交均價", "均價", "成本價", "買進均價"],
    "price": ["現價", "市價", "成交價", "最新價"],
    "kind": ["類別", "交易別", "種類", "資券別", "交易類別", "現股/融資"],
}
CODE_RE = re.compile(r"^\d{4}[A-Z]?$|^00\d{2,4}[A-Z]?$")


def _num(s: str) -> float | None:
    s = str(s).replace(",", "").replace("元", "").replace("股", "").replace("張", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _split(line: str) -> list[str]:
    return [c.strip() for c in (line.split("\t") if "\t" in line else re.split(r"\s{2,}|\s*\|\s*", line.strip()))]


def _find(header: list[str], keys: list[str]) -> int | None:
    for k in keys:  # 依關鍵字優先順序，先完全相等再包含
        for i, h in enumerate(header):
            if h == k:
                return i
    for k in keys:
        for i, h in enumerate(header):
            if k in h:
                return i
    return None


def parse(text: str) -> dict:
    lines = [ln for ln in text.replace("\r", "").split("\n") if ln.strip()]
    hdr_i, idx = None, {}
    for i, ln in enumerate(lines[:10]):
        cells = _split(ln)
        found = {k: _find(cells, v) for k, v in COLS.items()}
        if found["code"] is not None or (found["name"] is not None and (found["shares"] is not None or found["lots"] is not None)):
            hdr_i, idx = i, found
            break
    rows, skipped = [], []
    for ln in lines[(hdr_i + 1) if hdr_i is not None else 0:]:
        cells = _split(ln)
        get = lambda k: cells[idx[k]] if idx.get(k) is not None and idx[k] < len(cells) else None  # noqa: E731
        code = get("code")
        if not code or not CODE_RE.match(code.strip()):
            m = next((c for c in cells if CODE_RE.match(c)), None)  # 沒有代碼欄時，從整行找代碼
            code = m
        if not code:
            skipped.append(ln[:60])
            continue
        shares = _num(get("shares") or "") if idx.get("shares") is not None else None
        if shares is None and idx.get("lots") is not None:
            lots = _num(get("lots") or "")
            shares = lots * 1000 if lots is not None else None
        if shares is None or shares <= 0:
            skipped.append(ln[:60])
            continue
        kind = get("kind") or ""
        rows.append({"code": code.strip(), "name": get("name") or stock_meta(code.strip())["name"], "shares": int(round(shares)),
                     "cost": _num(get("cost") or ""), "price": _num(get("price") or ""), "margin": "資" in kind})
    merged: dict[str, dict] = {}
    for r in rows:  # 同一檔分現股／融資兩列時合併
        m = merged.setdefault(r["code"], {**r, "shares": 0})
        m["shares"] += r["shares"]
        m["margin"] = m["margin"] or r["margin"]
    return {"rows": list(merged.values()), "header_found": hdr_i is not None,
            "columns": {k: v for k, v in idx.items() if v is not None}, "skipped": skipped[:10]}


def reconcile(parsed: dict, complete: bool = True) -> dict:
    """與系統紀錄比較。complete=True 代表貼上的是完整庫存（系統有、XQ 沒有的視為已賣出）。"""
    pf = pfm.build_portfolio()
    xq = {r["code"]: r for r in parsed["rows"]}
    today = now_tw().date().isoformat()
    diffs, adjust = [], []
    for code in sorted(set(xq) | (set(pf.positions) if complete else set())):
        mine = pf.positions[code].shares if code in pf.positions else 0
        theirs = xq[code]["shares"] if code in xq else 0
        if mine == theirs:
            diffs.append({"code": code, "name": stock_meta(code)["name"], "system": mine, "xq": theirs, "status": "一致"})
            continue
        r = xq.get(code, {})
        price = r.get("cost") if theirs > mine else (r.get("price") or (pf.positions[code].avg_cost if code in pf.positions else None))
        diffs.append({"code": code, "name": stock_meta(code)["name"], "system": mine, "xq": theirs,
                      "status": "XQ 較多" if theirs > mine else "XQ 較少"})
        if price:
            adjust.append({"date": today, "code": code, "side": "BUY" if theirs > mine else "SELL", "shares": abs(theirs - mine),
                           "price": round(price, 2), "margin": int(bool(r.get("margin"))) if theirs > mine else 0,
                           "note": "XQ 對帳調整"})
        else:
            diffs[-1]["status"] += "（缺少價格，請手動記錄）"
    return {"diffs": diffs, "adjustments": adjust, "in_sync": not adjust and all(d["status"] == "一致" for d in diffs)}
