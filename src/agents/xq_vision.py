"""從 XQ 截圖讀取庫存或成交回報（XQ 的表格常無法複製時使用）。

流程：圖片縮到合理大小 → 視覺模型把表格轉成 JSON（Gemini 優先、NVIDIA 開源 VLM 備援；成交紀錄準確優先）→ 程式驗證。
防誤讀：
- 代號必須是系統認得的股票／ETF；模型讀到的名稱與代號對不起來時標示「請核對」
- 只回傳截圖上看得到的數字，看不清楚的欄位留空；不會自動寫入，一律由使用者確認
"""
from __future__ import annotations

import base64
import io
import logging
import os
import time

from pydantic import BaseModel, Field

from src.config import settings, stock_meta

log = logging.getLogger(__name__)

PROMPT = """這是台股下單軟體（XQ 全球贏家或券商 App）的截圖。判斷它是哪一種表格，並把表格逐列轉成 JSON。
- kind：庫存／持股／未實現損益表 → "holdings"；成交回報／委託回報／當日成交／對帳單 → "fills"；都不是 → "other"
- 每一列（一列就輸出一筆，同一檔分好幾列也不要合併）：
  code：股票代號（4～6 碼；常寫在名稱後的括號內，例如「艾笛森(3591)」→ 3591；ETF 可能 00 開頭或結尾有英文字母）
  name：商品名稱（不含括號代號）
  qty：數量欄的數字，照抄，不要換算（例如「20」→ 20、「345股」→ 345）
  unit：數量後面寫「股」→ "股"；寫「張」→ "張"；只有數字沒寫單位 → null
  qty_col：數量那一欄的表頭文字照抄（例如「成交數量」「庫存股數」「庫存張數」）
  board：盤別欄照抄（例如「整股」「盤中零股」「盤後零股」），沒有就 null
  price：holdings 填成本均價／持有均價；fills 填成交價／成交均價
  amount：成交價金／成交金額／付出成本（照抄，沒有就 null）
  side：fills 才填：買進／現股買進／融資買進 → "BUY"；賣出／現股賣出 → "SELL"
  kind：現股／融資／融券（例如「現股買進」→ 現股），看不到留空
  status：fills 才填，照抄狀態欄（例如「全部成交」「部分成交」「已刪單」），沒有狀態欄就 null
- 數字去掉千分位；看不清楚或表格沒有的欄位填 null，絕對不要猜
- 表頭、合計列、空白列不要輸出"""


class Row(BaseModel):
    code: str
    name: str | None = None
    qty: float | None = None
    unit: str | None = None
    qty_col: str | None = None
    board: str | None = None
    price: float | None = None
    amount: float | None = None
    side: str | None = None
    kind: str | None = None
    status: str | None = None


class Table(BaseModel):
    kind: str = Field(description="holdings / fills / other")
    rows: list[Row] = []


def to_shares(r: Row) -> tuple[int, str | None]:
    """數量換成股數，回傳 (股數, 警告)。依可靠程度決定單位：
    盤別（整股的數量是張、零股是股；模型有時會自己補單位，盤別最可靠）→ 數量後的「股」「張」→ 表頭（庫存股數／張數）→ 成交價金反推。
    單位已確定時，成交價金只用來核對（模型讀錯價金的機率比讀錯盤別高，不拿來推翻）。"""
    q = r.qty or 0
    if not q:
        return 0, None
    board = r.board or ""
    if "零股" in board:
        shares = q
    elif "整股" in board:
        shares = q * 1000
    elif r.unit == "股":
        shares = q
    elif r.unit == "張":
        shares = q * 1000
    elif r.qty_col and "股數" in r.qty_col:
        shares = q
    elif r.qty_col and "張" in r.qty_col:
        shares = q * 1000
    else:
        shares = None
    fits = []
    if r.amount and r.price:
        fits = [s for s in (q, q * 1000) if abs(s * r.price - r.amount) <= max(r.amount * 0.01, 5)]
    if shares is None:
        if len(fits) == 1:
            return int(fits[0]), None
        guess = int(q * 1000) if q < 1000 else int(q)  # 不帶單位的成交數量多半是張
        return guess, f"{r.code} 看不出數量單位，暫以 {guess:,} 股計，請核對"
    if r.amount and r.price and shares not in fits:
        return int(shares), f"{r.code} {int(shares):,} 股 × {r.price:g} 與截圖價金 {r.amount:,.0f} 對不上，請核對"
    return int(shares), None


def _shrink(data: bytes, max_side: int = 1600) -> tuple[bytes, str]:
    """長邊縮到 1600px、轉 JPEG（NVIDIA 內嵌圖片有大小限制，手機全螢幕截圖也能放得下）。"""
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    img = img.convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side))
    for q in (90, 80, 70, 60):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q)
        if buf.tell() <= 170_000 or q == 60:
            return buf.getvalue(), "image/jpeg"
    return buf.getvalue(), "image/jpeg"


def _nvidia(model: str, b64: str, mime: str) -> str:
    from src.llm import openai_compat as oc

    msg = oc.chat(oc.PROVIDERS["nvidia"]["base_url"], os.environ["NVIDIA_API_KEY"], model,
                  [{"role": "user", "content": [{"type": "text", "text": PROMPT + "\n只輸出 JSON：{\"kind\":…,\"rows\":[…]}"},
                                               {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}],
                  temperature=0, max_tokens=4096, thinking=False, timeout=60)
    return oc.extract_json(msg["content"])


def _gemini(model: str, data: bytes, mime: str) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    r = client.models.generate_content(
        model=model, contents=[types.Part.from_bytes(data=data, mime_type=mime), PROMPT],
        config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json", response_schema=Table))
    return r.text


def read_table(data: bytes) -> tuple[Table, str]:
    """回傳 (表格, 使用的模型)；全部模型都失敗時丟 RuntimeError。"""
    img, mime = _shrink(data)
    b64 = base64.b64encode(img).decode()
    cfg = settings()["llm"]
    tries = []
    if os.getenv("GEMINI_API_KEY"):  # 截圖是成交紀錄，準確優先：實測 Gemini 較準，排第一
        tries += [("gemini", m) for m in cfg.get("gemini_vision_models", [])]
    if os.getenv("NVIDIA_API_KEY"):
        tries += [("nvidia", m) for m in cfg.get("nvidia_vision_models", [])]
    errors = []
    for prov, model in tries:
        t = time.time()
        try:
            raw = _nvidia(model, b64, mime) if prov == "nvidia" else _gemini(model, img, mime)
            table = Table.model_validate_json(raw)
            log.info("截圖辨識 %s：%s，%d 列，%.1f 秒", model, table.kind, len(table.rows), time.time() - t)
            return table, model
        except Exception as e:  # noqa: BLE001 - 換下一個模型
            errors.append(f"{model}: {str(e)[:120]}")
            log.warning("截圖辨識失敗 %s：%s", model, e)
    raise RuntimeError("；".join(errors) or "未設定可讀圖片的模型（NVIDIA_API_KEY 或 GEMINI_API_KEY）")


def _clean_code(code: str) -> str:
    return code.strip().upper().replace(" ", "").replace(".TW", "").replace(".TWO", "")


def check_rows(table: Table) -> tuple[list[dict], list[str]]:
    """驗證並整理成 xq_import 的列格式（每列一筆，不合併）；回傳 (列, 警告)。"""
    rows, warns = [], []
    for r in table.rows:
        code = _clean_code(r.code or "")
        meta = stock_meta(code)
        known = bool(meta.get("name")) and meta["name"] != code
        if not known:
            warns.append(f"{code}（{r.name or '?'}）不是系統認得的代號，已略過，請手動記錄")
            continue
        if r.name and meta["name"].rstrip("*") not in r.name and r.name not in meta["name"]:
            warns.append(f"{code} 截圖名稱「{r.name}」與系統「{meta['name']}」不同，請核對代號是否讀錯")
        shares, w = to_shares(r)
        if w:
            warns.append(w)
        rows.append({"code": code, "name": meta["name"], "read_name": r.name, "shares": shares, "board": r.board,
                     "cost": r.price, "price": r.price, "amount": r.amount, "side": (r.side or "").upper() or None,
                     "margin": "資" in (r.kind or ""), "status": r.status, "filled": shares})
    return rows, warns
