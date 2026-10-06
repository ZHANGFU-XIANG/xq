"""XQ 匯出的 CSV（委託回報／成交回報）→ 成交紀錄草稿。數字全由程式解析，不經 LLM。

XQ 委託回報欄位（Big5）：委託時間, 委託書號, 盤別, 交易類別, 委託種類, 股票名稱, 原委託, 價格, 取消, 成交, 成交均價, 來源別, 交易日期, 狀態
- 股票名稱為「國票金(2889)」，代號在括號內
- 盤別「整股」的數量單位是張；「盤中零股／盤後零股」的數量帶「股」字，單位是股
- 只取「成交」> 0 的列（部分成交取成交量）；委託成功、刪單、失敗的略過
- 成交價：有「成交均價」用成交均價；沒有（委託回報常見）就用委託價，並提醒使用者核對
- 與既有紀錄比對：同日期、代號、買賣、股數視為已記錄（委託價可能和實際成交價不同，所以不比價格）

XQ 期貨委託回報欄位：委託時間, 委託書號, 交易種類(期貨), 買賣別, 商品名稱(台股指數106), 倉別(新倉/平倉), 委託條件, 委託價格, 委託數量, 取消, 成交, …
- 商品：台股指數／台指 → TX 大台、小型台指 → MTX、微型台指 → TMF；結尾數字「106」＝ 10 月、年份尾數 6（2026-10）
- 對應是依 XQ 命名慣例推測，預覽時可修改商品與月份；選擇權格式未知，請手動記錄
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter

from src.config import stock_meta
from src.state import portfolio as pfm

COLS = {
    "time": ("委託時間", "成交時間", "時間"),
    "kind": ("交易種類", "商品類別"),
    "open_close": ("倉別", "新平倉"),
    "board": ("盤別",),
    "type": ("交易類別", "買賣別", "買賣", "交易別"),
    "name": ("股票名稱", "商品名稱", "商品", "股票"),
    "code": ("股票代號", "商品代碼", "代號", "代碼"),
    "order_qty": ("原委託", "委託數量", "委託量"),
    "limit": ("價格", "委託價", "委託價格"),
    "filled": ("成交", "成交數量", "成交股數", "成交量"),
    "avg": ("成交均價", "成交單價", "成交價", "成交價格"),
    "order_no": ("委託書號", "委託單號", "書號"),
    "amount": ("成交價金",),
    "date": ("交易日期", "成交日期", "日期"),
    "status": ("狀態", "委託狀態"),
}


def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp950", "big5hkscs"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _num(s: str) -> float | None:
    s = re.sub(r"[^\d.\-]", "", s or "")
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None


def _col(header: list[str], key: str) -> int | None:
    names = COLS[key]
    for want in names:  # 完全相同優先（「成交」不能誤配到「成交均價」）
        for i, h in enumerate(header):
            if h == want:
                return i
    for want in names:
        for i, h in enumerate(header):
            if want in h and not (key == "filled" and "均價" in h) and not (key == "filled" and "價" in h):
                return i
    return None


def _side(text: str) -> tuple[str | None, bool]:
    """交易類別 → (BUY/SELL, 是否融資)。融券、借券不支援。"""
    t = text or ""
    if "券" in t:
        return None, False
    margin = "融資" in t
    if "買" in t:
        return "BUY", margin
    if "賣" in t:
        return "SELL", margin
    return None, False


def parse(raw: bytes, date_map: dict[str, str] | None = None) -> dict:
    """date_map：{委託書號: YYYY-MM-DD}，成交回報沒有日期時用來補（由同時上傳的委託回報取得）。"""
    text = decode(raw)
    rows = list(csv.reader(io.StringIO(text)))
    rows = [[c.strip().strip("\t").strip() for c in r] for r in rows if any(c.strip() for c in r)]
    if not rows:
        return {"error": "CSV 是空的"}
    header = rows[0]
    idx = {k: _col(header, k) for k in COLS}
    if idx["name"] is None and idx["code"] is None:
        return {"error": f"看不出是 XQ 委託／成交回報 CSV（找不到股票名稱或代號欄位）。表頭：{'、'.join(header[:10])}"}
    if idx["filled"] is None or idx["type"] is None:
        return {"error": "CSV 缺少「成交」或「交易類別」欄位，請匯出 XQ 的委託回報或成交回報"}

    def get(r, k):
        i = idx[k]
        return r[i] if i is not None and i < len(r) else ""

    out, skipped, warns = [], [], []
    used_limit = False
    is_fills = idx["order_qty"] is None and idx["avg"] is not None  # 成交回報：每列一筆成交、有成交單價
    nodate = 0
    body = rows[1:][::-1] if is_fills else rows[1:]  # 成交回報由新到舊排列，反轉成時間順序（同日先進先出）
    for seq, r in enumerate(body):
        name_cell = get(r, "name")
        kind_cell = get(r, "kind")
        if "選擇權" in kind_cell or "選擇權" in name_cell:
            skipped.append(f"{name_cell}：選擇權請在上方表單手動記錄（{get(r, 'status')}）")
            continue
        fut = _future(name_cell) if ("期貨" in kind_cell or _future(name_cell)) else None
        if fut is not None:
            row = _future_row(r, get, name_cell, fut, skipped)
            if row:
                used_limit = used_limit or row["price_source"] == "委託價"
                out.append(row)
            continue
        m = re.search(r"\(([0-9A-Z]{4,6})\)", name_cell)
        code = (get(r, "code") or (m.group(1) if m else "")).strip().upper()
        name = re.sub(r"\*?\s*\([0-9A-Z]+\)\s*$", "", name_cell).strip("* ") or (stock_meta(code).get("name") if code else "")
        status = get(r, "status")
        side, margin = _side(get(r, "type"))
        board = get(r, "board")
        filled_raw = get(r, "filled")
        filled = _num(filled_raw) or 0
        label = f"{code} {name}".strip()
        if not code:
            skipped.append(f"{name_cell or '（空白）'}：找不到股票代號（期貨／選擇權請在交易紀錄頁手動記錄）")
            continue
        if side is None:
            skipped.append(f"{label}：{get(r, 'type') or '未知交易類別'}（只支援現股、融資買賣）")
            continue
        if filled <= 0:
            skipped.append(f"{label}：{status or '未成交'}")
            continue
        odd = "零股" in board or "股" in filled_raw
        shares = int(round(filled)) if odd else int(round(filled * 1000))
        avg = _num(get(r, "avg"))
        price = avg or _num(get(r, "limit"))
        if not price:
            skipped.append(f"{label}：缺少價格")
            continue
        if not avg:
            used_limit = True
        d = (get(r, "date") or get(r, "time")[:10]).replace("/", "-")[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            d = (date_map or {}).get(get(r, "order_no"), "")  # 成交回報的時間只有分秒 → 用委託書號對日期
            if not d:
                nodate += 1
        out.append({"date": d, "time": f"{seq:05d}" if is_fills else get(r, "time"), "code": code, "name": name, "side": side, "margin": margin,
                    "order_no": get(r, "order_no"), "amount_file": _num(get(r, "amount")),
                    "shares": shares, "price": round(price, 4), "price_source": "成交均價" if avg else "委託價",
                    "board": "零股" if odd else "整股", "status": status,
                    "partial": bool(_num(get(r, "order_qty")) and filled < (_num(get(r, "order_qty")) or 0))})
    out.sort(key=lambda x: (x["date"] or "9999", x["time"]))  # 依時間由早到晚，先進先出才正確
    if used_limit:
        warns.append("這份 CSV 沒有成交均價，價格先用「委託價」。實際成交價可能較好（例如 10/05 聯茂零股委託 724、實際 723），"
                     "請對照 XQ 成交回報修改後再匯入，或匯入後在列表按「修改」。")
    if any(x["partial"] for x in out):
        warns.append("有部分成交的委託，只記錄已成交的數量。")
    if nodate:
        warns.append(f"{nodate} 筆成交沒有日期（成交回報只有時間）：請同時選擇委託回報 CSV 以委託書號對出日期，或在下方逐筆填寫。")
    return {"rows": out, "skipped": skipped, "warnings": warns, "columns": header, "kind": "fills" if is_fills else "orders"}


def order_dates(raw: bytes) -> dict[str, str]:
    """委託回報 → {委託書號: 交易日期}。"""
    rows = [[c.strip().strip("	").strip() for c in r] for r in csv.reader(io.StringIO(decode(raw))) if any(c.strip() for c in r)]
    if not rows:
        return {}
    i_no, i_d = _col(rows[0], "order_no"), _col(rows[0], "date")
    if i_no is None or i_d is None:
        return {}
    out = {}
    for r in rows[1:]:
        if max(i_no, i_d) < len(r):
            d = r[i_d].replace("/", "-")[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                out[r[i_no]] = d
    return out


def parse_many(files: list[bytes]) -> dict:
    """一次匯入多個 CSV：有成交回報就只用成交回報記帳（委託回報只用來補日期，避免重複計算）。"""
    dmap: dict[str, str] = {}
    for raw in files:
        dmap.update(order_dates(raw))
    parsed = [parse(raw, dmap) for raw in files]
    errors = [p["error"] for p in parsed if p.get("error")]
    ok = [p for p in parsed if not p.get("error")]
    if not ok:
        return {"error": "；".join(errors) or "沒有可匯入的資料"}
    use = [p for p in ok if p["kind"] == "fills"] or ok
    rows = sorted([r for p in use for r in p["rows"]], key=lambda x: (x["date"] or "9999", x["time"]))
    notes = [w for p in use for w in p["warnings"]]
    if len(use) < len(ok):
        notes.append(f"同時有委託回報與成交回報：以成交回報（實際成交價）記帳，委託回報只用來對日期（對到 {len(dmap)} 筆委託書號）。")
    return {"rows": rows, "skipped": [x for p in use for x in p["skipped"]], "warnings": list(dict.fromkeys(notes)) + errors,
            "kinds": [p["kind"] for p in ok]}


def _future(name: str) -> dict | None:
    """「台股指數106」→ {root: TX, ym: 202610}；看不出來回 None。"""
    n = name.replace(" ", "")
    if "微型" in n or "微台" in n:
        root = "TMF"
    elif "小型" in n or "小台" in n or "小臺" in n:
        root = "MTX"
    elif any(k in n for k in ("台股指數", "臺股指數", "台指", "臺指", "台股期", "臺股期")):
        root = "TX"
    else:
        return None
    ym = None
    m = re.search(r"(\d{3,6})$", n)
    if m:
        d = m.group(1)
        if len(d) == 3 and 1 <= int(d[:2]) <= 12:  # MM + 年份尾數
            ym = f"202{d[2]}{d[:2]}"
        elif len(d) == 4 and 1 <= int(d[2:]) <= 12:  # YYMM
            ym = f"20{d[:2]}{d[2:]}"
        elif len(d) == 6:
            ym = d
        if ym and not "2026" <= ym[:4] <= "2027":  # 不在競賽期間附近 → 讀錯，留給使用者填
            ym = None
    return {"root": root, "ym": ym}


def _future_row(r, get, name_cell: str, fut: dict, skipped: list[str]) -> dict | None:
    side, _ = _side(get(r, "type"))
    filled = _num(get(r, "filled")) or 0
    if side is None:
        skipped.append(f"{name_cell}：看不出買賣方向")
        return None
    if filled <= 0:
        skipped.append(f"{name_cell}：{get(r, 'status') or '未成交'}")
        return None
    avg = _num(get(r, "avg"))
    price = avg or _num(get(r, "limit"))
    if not price:
        skipped.append(f"{name_cell}：缺少價格（市價單請手動記錄成交點數）")
        return None
    d = (get(r, "date") or get(r, "time")[:10]).replace("/", "-")[:10]
    return {"kind": "FUT", "date": d, "time": get(r, "time"), "code": fut["root"], "name": name_cell, "side": side,
            "order_no": get(r, "order_no"), "amount_file": None,
            "margin": False, "shares": int(round(filled)), "price": round(price, 4),
            "contract": f"{fut['root']}{fut['ym']}" if fut["ym"] else "", "ym": fut["ym"] or "",
            "price_source": "成交均價" if avg else "委託價", "board": get(r, "open_close") or "期貨", "status": get(r, "status"),
            "partial": bool(_num(get(r, "order_qty")) and filled < (_num(get(r, "order_qty")) or 0))}


def mark_duplicates(rows: list[dict]) -> list[dict]:
    """與既有交易紀錄比對：同日期、代號、買賣、股數視為已記錄（不比價格，因為委託價可能不等於成交價）。"""
    have = Counter((t["date"], (t.get("contract") or t["code"]).strip().upper(), t["side"].strip().upper(), int(float(t["shares"])))
                   for t in pfm.load_trades() if t["code"].strip().upper() not in ("FEE", "TRANSFER"))
    for r in rows:
        r.setdefault("kind", "STOCK")
        k = (r["date"], r.get("contract") or r["code"], r["side"], r["shares"])
        r["duplicate"] = have[k] > 0
        if r["duplicate"]:
            have[k] -= 1
    return rows
