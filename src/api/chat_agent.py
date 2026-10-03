"""ChatAgent：AI 助理。Gemini function calling（Groq 備援），只根據工具回傳的程式計算結果回答。

防幻覺：
- 工具全部回傳程式計算的數字；系統提示要求任何數字都必須來自工具結果
- 最終回答用 verifier 核對（事實＝本輪所有工具輸出＋使用者訊息），不通過重寫一次，仍有未核實數字則標記「〔未核實〕」
- 寫入類動作（記錄成交、重跑分析）只建立「待確認動作」，使用者在前端按確認才執行
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from collections.abc import Iterator

from src.agents import on_demand
from src.agents.verifier import NUM_RE, fact_numbers, verify
from src.api import data as apidata
from src.config import now_tw, rules, settings
from src.llm.client import get_client, mark_exhausted, model_ok

log = logging.getLogger(__name__)
MAX_STEPS = 6
PENDING: dict[str, dict] = {}

SYSTEM = """你是「2026 德明X富達投資擂台」參賽隊伍的投資顧問助理，用專業、精簡、自然的繁體中文（台灣用語）回答，像資深營業員或研究員對客戶說明。

服務範圍：
只回答投資、股市、經濟、金融、產業與公司營運、競賽規則、本隊持股與交易相關的問題，以及和這些主題高度相關的問題（例如國際局勢、政策對市場的影響）。
其他問題（閒聊、寫作、翻譯、程式、感情、娛樂、健康、法律等）一律婉拒：用一到兩句話說明這裡只提供投資與經濟相關的協助，並舉一個可以改問的例子；婉拒時不要呼叫工具。

誠實與出處（最高優先）：
A. 每個工具結果都有 source_id（例如 S1）。每個論點、數字、事件，都要在句末標註出處，格式「〔S1〕」；同一句用到多個來源寫「〔S1〕〔S3〕」。沒有出處可標的內容不要寫。
B. 只能使用本輪工具結果或前文已核實內容裡的數字與事件；禁止估算、憑記憶、編造數據或描述工具沒有回傳的新聞事件。提到新聞要寫出新聞標題重點與日期。
C. 因果關係只有在資料直接支持時才能下結論。資料只顯示數字時，只陳述數字本身，不要加上「顯示市場情緒」「代表資金湧入」這類解讀；真的需要解讀時，句首寫「推論：」。例如「法人賣超、股價同期下跌」只能寫成兩者同時發生，不能寫成因為賣超所以下跌。
D. 模型的機率、預期報酬、預測開盤都要說明是「模型估計，不是保證」。
E. 資料不足、互相矛盾或無法證實的部分，獨立一行以「無法確認：」開頭說明缺什麼；完全沒有資料時直接回答「目前沒有這項資料，無法回答」並說明原因（例如尚未公布），再提供工具查得到的最接近資料（例如最近已公布的數字），不要猜。

寫作格式：
- 不使用粗體、井字號標題、表情符號、分隔線、HTML 標籤，也不要用「當然」「好的」「以下是」「希望對你有幫助」「總結來說」「值得注意的是」這類客套或制式用語。
- 第一句直接給結論；接著用「1. 2. 3.」列出依據（每點附出處）；最後一行以「建議：」開頭寫具體動作。比較多檔時可以用簡單表格。
- 註明資料日期。不要輸出 JSON 或英文欄位名稱。

工具使用規則：
1. 回答任何股價、張數、金額、比例、機率等數字前，必須先呼叫工具取得；只能使用工具結果中的數字，禁止自行估算或憑記憶。
2. 使用者問到任何股票（包含每日建議沒有的），先用 find_stock 找代號，再用 analyze_stock 取得分析與操作建議。
2b. 問公司在做什麼、財報、EPS、營收、股利、重大訊息時，用 company_profile（公開資訊觀測站官方資料）。
3. 給買進建議時，要說明：屬於進攻籃或穩健籃與原因、建議股數（張＋零股）、限價、停損、出場計畫、主要風險。
4. 使用者說已成交（例如「我買了 2330 一張 1000 元」）時呼叫 record_trade；要重新產生報告時呼叫 run_analysis。這兩個工具只會建立待確認動作，請提醒使用者在畫面上按「確認」。
5. 「如果買/賣…會怎樣」用 simulate_trades 試算，說明成本、持股率與競賽規則影響。
5c. 使用者貼上看起來像 XQ 庫存表格的內容時，呼叫 import_xq_holdings（text 原樣傳入），說明差異並提醒按確認同步。
5d. 問到手上股票要不要加碼、續抱、減碼或賣掉時，呼叫 get_holding_advice，照工具給的結論、股數與條件回答。
5b. 問到警示、漲跌停、負面新聞、要不要賣、該賣多少時，先呼叫 get_alerts，照工具給的建議股數與理由回答。
6. 競賽重點：持股率需 ≥70%（每日收盤檢查）、至少 5 檔、10 筆交易、禁止權證、只有正報酬才排名。
5g. 問 ETF 時用 get_etf_view，比例一律用 today_etf_share_pct（今日實際比例），不要把「基準比例」當成今日比例；個別 ETF 用 analyze_stock（含折溢價、類別、ETF 籃張數）；問期貨、台指期、選擇權、避險時用 get_futures_view，並照實說明回測有沒有優勢；沒有優勢就直說不建議。
5h. 使用者說期貨／選擇權成交時，用 record_trade 並填 contract（例如 TMF202610、TXO202610P46000）；說轉帳到期貨戶或證券戶時用 record_transfer。
5e. 問「還有什麼可以注意／潛在標的／候補」時呼叫 get_potential_picks；問某檔為什麼沒被推薦時，用 analyze_stock，依其中 today_status 回答：排第幾名、和最後一檔入選者的分數差距、拖累分數的項目（weakest_components 與 risks），不要自行推測其他原因。
5f. 這是連續對話：使用者說「它」「這檔」「剛剛那檔」「第二個」時，指前文提到的股票或項目，直接沿用，不要再問一次；需要數字時仍要重新呼叫工具。
7. 問到新聞、事件、國際局勢對股票的影響時，用 search_news 或 get_macro 取得真實新聞，並用 analyze_stock 的國際敏感度或歸因數據佐證影響幅度；沒有數據支持的影響不要寫。"""

# 出處說明：讓使用者知道每個 source_id 背後是哪個資料來源
SOURCE_DESC = {
    "find_stock": "證交所／櫃買中心上市櫃清單",
    "stock_profile": "證交所／櫃買中心日成交資料、FinMind（本益比、月營收、法人買賣超、融資券）",
    "company_profile": "公開資訊觀測站",
    "analyze_stock": "本系統模型計算（資料：證交所／櫃買日 K、FinMind 法人與月營收、Yahoo Finance 美股收盤）",
    "get_daily_report": "本系統每日盤前報告",
    "get_portfolio": "本隊成交紀錄（系統登錄）",
    "search_news": "新聞（Google 新聞、鉅亨網等，附原文連結）",
    "get_macro": "本系統國際大事分析（新聞＋Yahoo Finance 國際市場收盤）",
    "get_rules": "競賽官方規則（德明財經科技大學競賽網站）",
    "get_holding_advice": "本系統持股建議（證交所即時報價計算）",
    "get_alerts": "證交所／櫃買中心即時報價、注意與處置股公告、新聞",
    "get_model_health": "本系統模型回測與實際命中紀錄",
    "simulate_trades": "本系統試算（競賽手續費 0.1425%、證交稅）",
    "get_potential_picks": "本系統每日排名",
    "get_data_sources": "本系統資料來源監控",
    "get_etf_view": "本系統 ETF 籃計算（證交所 ETF 預估淨值、一個月模型）",
    "get_futures_view": "本系統期貨建議（期交所 MIS 即時報價與 OpenAPI 保證金、加權指數 5 年回測）",
    "record_transfer": "待確認動作",
    "get_futures_quote": "期交所 MIS 即時報價與 OpenAPI 保證金",
    "record_trade": "待確認動作", "run_analysis": "待確認動作", "import_xq_holdings": "使用者貼上的 XQ 庫存",
}
RULES_URL = "https://sites.google.com/gs.takming.edu.tw/2026pmcxc/競賽專區/競賽規則"


def _tag(out, n: int, name: str):
    """在工具結果最前面加上出處編號，模型引用時寫〔S{n}〕。"""
    if not isinstance(out, dict):
        out = {"result": out}
    return {"source_id": f"S{n}", "source": SOURCE_DESC.get(name, name), **out}


def _links(name: str, r: dict) -> list[dict]:
    """出處可點的原文連結（新聞、觀測站、競賽規則）。"""
    if name == "search_news":
        return [{"title": n.get("title", ""), "url": n["url"]} for n in r.get("news", [])[:6] if n.get("url")]
    if name == "analyze_stock":
        return [{"title": n.get("title", ""), "url": n["url"]} for n in (r.get("news") or [])[:3] if n.get("url")]
    if name == "get_macro":
        return [{"title": n.get("title", ""), "url": n["url"]} for e in (r.get("macro") or [])[:3]
                for n in (e.get("news") or [])[:1] if n.get("url")]
    if name == "company_profile":
        return [{"title": "公開資訊觀測站", "url": "https://mops.twse.com.tw/mops/#/web/t146sb05"}]
    if name == "get_rules":
        return [{"title": "競賽規則", "url": RULES_URL}]
    return []


_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF️]")
_FILLERS = re.compile(r"^(當然|好的|沒問題|以下是[^。：]*[。：]?|希望(以上|這些)?對你有(所)?幫助[。！]?)\s*")


def _style(text: str) -> str:
    """去除 AI 慣用符號：粗體、井字號標題、分隔線、HTML 換行、表情符號、制式開場白。"""
    lines = []
    for ln in text.replace("<br>", "；").replace("<br/>", "；").splitlines():
        if re.fullmatch(r"\s*([-*_]\s*){3,}\s*", ln):
            continue
        ln = re.sub(r"^\s{0,3}#{1,6}\s*", "", ln)
        ln = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), ln)
        ln = _EMOJI.sub("", ln)
        lines.append(ln.rstrip())
    out = "\n".join(lines).strip()
    out = _FILLERS.sub("", out)
    return re.sub(r"\n{3,}", "\n\n", out)


_DIG = str.maketrans("0123456789", "abcdefghij")
_UNDIG = str.maketrans("abcdefghij", "0123456789")


def _hide_cites(text: str) -> str:
    """出處編號〔S12〕暫時改成不含數字的〔Sbc〕，避免被數字核對當成數據。"""
    return re.sub(r"〔S(\d+)〕", lambda m: f"〔S{m.group(1).translate(_DIG)}〕", text)


def _show_cites(text: str) -> str:
    return re.sub(r"〔S([a-j]+)〕", lambda m: f"〔S{m.group(1).translate(_UNDIG)}〕", text)


MODEL_TOOLS = {"analyze_stock", "get_daily_report", "get_potential_picks", "get_model_health", "stock_profile",
               "get_holding_advice", "get_macro"}


def _flag_uncited(text: str) -> tuple[str, int]:
    """條列的每一點都要有出處；沒有出處、也沒標明「推論／無法確認」的，標示〔未附出處〕。"""
    n = 0
    out = []
    for ln in text.splitlines():
        body = re.sub(r"^\s*(\d+[.、]|[-*])\s*", "", ln)
        is_point = body != ln.lstrip() or re.match(r"^\s*(\d+[.、]|[-*])\s", ln)
        if is_point and body and "〔S" not in ln and not body.startswith(("推論", "無法確認", "建議")):
            ln = ln.rstrip() + "〔未附出處〕"
            n += 1
        out.append(ln)
    return "\n".join(out), n


def _check_cites(text: str, n_sources: int) -> tuple[str, list[str]]:
    """引用不存在的出處編號 → 標示「出處不明」。"""
    bad = []

    def repl(m):
        i = int(m.group(1))
        if 1 <= i <= n_sources:
            return m.group(0)
        bad.append(f"S{i}")
        return "〔出處不明〕"

    return re.sub(r"〔S(\d+)〕", repl, text), bad

TOOLS = [
    {"name": "find_stock", "description": "用股票名稱或代號搜尋台股（上市櫃、ETF）",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "stock_profile", "description": "個股基本盤：價量、52 週高低、本益比/淨值比/殖利率、月營收、法人、融資券、模型分數",
     "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "company_profile", "description": "公開資訊觀測站公司資料：主要業務、董事長、資本額、成立／上市日、近 8 季營收毛利率營益率 EPS、最新財報摘要、月營收、重大訊息、內部人持股、股利",
     "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "analyze_stock", "description": "個股完整分析與操作建議：進攻/穩健雙籃分數與理由、一個月模型、隔夜預測、今日漲跌歸因、國際因子敏感度、新聞，以及買進張數或持股賣出建議",
     "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "get_daily_report", "description": "取得某日（預設最新）每日報告重點：攻守配置、交易建議、規則檢查、大盤預測、排行",
     "parameters": {"type": "object", "properties": {"date": {"type": "string", "description": "YYYY-MM-DD，可省略"}}}},
    {"name": "get_portfolio", "description": "目前持股、現金、總資產、報酬率、持股率、交易紀錄",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "search_news", "description": "搜尋新聞（個股名稱或關鍵字），回傳標題與連結",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "get_macro", "description": "最新國際大事與對持股的影響金額、後續決策",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_rules", "description": "競賽規則", "parameters": {"type": "object", "properties": {}}},
    {"name": "get_holding_advice", "description": "庫存中每檔股票的建議：加碼／續抱／減碼／全部出清，含股數、限價、理由與停損停利條件（依目前庫存與即時價計算）",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_alerts", "description": "即時警示：持股跌停／跌破停損／負面新聞與建議賣出股數、今日建議買進前的風險、全市場漲跌停與原因、注意／處置股",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_model_health", "description": "模型回測表現、實際命中率與調整 Agent 紀錄",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "simulate_trades", "description": "試算一組假設買賣後的現金、交易成本、持股率、雙籃比例與規則檢查（不會真的下單或記錄）",
     "parameters": {"type": "object", "properties": {"trades": {"type": "array", "items": {"type": "object", "properties": {
         "code": {"type": "string"}, "side": {"type": "string", "enum": ["BUY", "SELL"]}, "shares": {"type": "integer"},
         "price": {"type": "number"}, "margin": {"type": "boolean"}}, "required": ["code", "side", "shares"]}}},
         "required": ["trades"]}},
    {"name": "record_trade", "description": "記錄在競賽平台的實際成交（建立待確認動作，使用者確認後才寫入）",
     "parameters": {"type": "object", "properties": {
         "code": {"type": "string", "description": "股票／ETF 代號；期貨填 TX、MTX、TMF；選擇權填 TXO"},
         "side": {"type": "string", "enum": ["BUY", "SELL"]},
         "shares": {"type": "integer", "description": "股數；期貨與選擇權為口數"},
         "price": {"type": "number", "description": "成交價；期貨為點數、選擇權為權利金點數"}, "margin": {"type": "boolean"},
         "contract": {"type": "string", "description": "期貨／選擇權合約，例如 TMF202610、TXO202610P46000"},
         "date": {"type": "string", "description": "YYYY-MM-DD，預設今天"}},
         "required": ["code", "side", "shares", "price"]}},
    {"name": "record_transfer", "description": "記錄證券戶與期貨戶之間的資金調撥（建立待確認動作）",
     "parameters": {"type": "object", "properties": {
         "direction": {"type": "string", "enum": ["TO_FUTURES", "TO_SECURITIES"], "description": "TO_FUTURES＝證券戶轉入期貨戶"},
         "amount": {"type": "number"}, "date": {"type": "string"}}, "required": ["direction", "amount"]}},
    {"name": "get_etf_view", "description": "ETF 籃建議：今日配置比例與理由、候選 ETF 的類別、分數、折溢價、建議張數",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_futures_quote", "description": "期貨／選擇權行情與規格：TX 大台、MTX 小台、TMF 微台的各月報價、基差、保證金、每點價值、槓桿、結算日；TXO 則回傳賣權報價表",
     "parameters": {"type": "object", "properties": {"root": {"type": "string", "enum": ["TX", "MTX", "TMF", "TXO"]}}, "required": ["root"]}},
    {"name": "get_futures_view", "description": "期貨與選擇權建議：方向模型機率、回測是否有優勢、模式（衝名次／補回虧損／保名次）、建議口數、保證金、賣權避險",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "import_xq_holdings", "description": "解析使用者從 XQ 全球贏家庫存畫面複製貼上的表格，與系統持股比對，產生需確認的同步調整",
     "parameters": {"type": "object", "properties": {"text": {"type": "string", "description": "使用者貼上的原始表格文字"}},
                    "required": ["text"]}},
    {"name": "get_potential_picks", "description": "潛在標的：模型看好但今天沒列入下單的股票（候補遞補、過熱等回檔），含排名、分數、為什麼還沒買、觸發條件",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_data_sources", "description": "各資料來源目前狀態：FinMind 各組 token 是否冷卻、證交所／櫃買中心官方來源最近成功或失敗時間",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "run_analysis", "description": "重新執行分析並產生報告（建立待確認動作）",
     "parameters": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["premarket", "postclose", "weekly", "tune"]}},
                    "required": ["mode"]}},
]


# ---------------------------------------------------------------- 工具實作
def _record(args: dict) -> dict:
    from src.agents.compliance import is_warrant
    from src.state.portfolio import DERIV_ROOTS, asset_of

    code = str(args["code"]).strip().upper()
    if is_warrant(code):
        return {"error": "權證不可交易（競賽規則）"}
    row = {"date": args.get("date") or now_tw().date().isoformat(), "code": code, "side": args["side"].upper(),
           "shares": int(args["shares"]), "price": float(args["price"]), "margin": int(bool(args.get("margin"))),
           "note": "AI 助理記錄", "contract": str(args.get("contract") or "").upper()}
    row["asset"] = asset_of(row)
    if code in DERIV_ROOTS:
        if not row["contract"].startswith(code) or len(row["contract"]) < len(code) + 6:
            return {"error": f"期貨／選擇權請提供完整合約，例如 {code}202610" + ("P46000" if code == "TXO" else "")}
        name = row["contract"]
        aid = uuid.uuid4().hex[:10]
        PENDING[aid] = {"kind": "record_trade", "payload": row, "created": time.time()}
        return {"pending_action": aid, "summary": f"記錄成交：{row['date']} {'買進' if row['side'] == 'BUY' else '賣出'} "
                f"{name} {row['shares']} 口 @ {row['price']}", "status": "等待使用者在畫面上按確認"}
    name = on_demand.stock_meta(code)["name"]
    aid = uuid.uuid4().hex[:10]
    PENDING[aid] = {"kind": "record_trade", "payload": row, "created": time.time()}
    return {"pending_action": aid, "summary": f"記錄成交：{row['date']} {'買進' if row['side'] == 'BUY' else '賣出'} "
            f"{code} {name} {row['shares']:,} 股 @ {row['price']}" + ("（融資）" if row["margin"] else ""),
            "status": "等待使用者在畫面上按確認"}


def xq_sync_preview(text: str, complete: bool = True) -> dict:
    """解析 XQ 貼上的庫存並與系統比對；有差異時建立待確認動作。"""
    from src.agents import xq_import

    parsed = xq_import.parse(text)
    if not parsed["rows"]:
        return {"error": "沒有解析到任何持股。請在 XQ 庫存畫面全選（Ctrl+A）後複製（Ctrl+C），連同表頭一起貼上。",
                "skipped": parsed["skipped"]}
    rec = xq_import.reconcile(parsed, complete)
    out = {"parsed": parsed["rows"], "columns": parsed["columns"], **rec}
    if rec["adjustments"]:
        aid = uuid.uuid4().hex[:10]
        PENDING[aid] = {"kind": "xq_sync", "payload": rec["adjustments"], "created": time.time()}
        out["pending_action"] = aid
        out["summary"] = "依 XQ 庫存同步：" + "、".join(
            f"{'補記買進' if a['side'] == 'BUY' else '補記賣出'} {a['code']} {a['shares']:,} 股@{a['price']}" for a in rec["adjustments"])
    return out


def _transfer(args: dict) -> dict:
    amt = float(args["amount"])
    if amt <= 0:
        return {"error": "金額必須大於 0"}
    row = {"date": args.get("date") or now_tw().date().isoformat(), "code": "TRANSFER", "side": args["direction"],
           "shares": 1, "price": amt, "margin": 0, "note": "資金調撥", "asset": "CASH", "contract": ""}
    aid = uuid.uuid4().hex[:10]
    PENDING[aid] = {"kind": "record_trade", "payload": row, "created": time.time()}
    text = "證券戶 → 期貨戶" if args["direction"] == "TO_FUTURES" else "期貨戶 → 證券戶"
    return {"pending_action": aid, "summary": f"記錄資金調撥：{row['date']} {text} {amt:,.0f} 元", "status": "等待使用者在畫面上按確認"}


def _run(args: dict) -> dict:
    aid = uuid.uuid4().hex[:10]
    names = {"premarket": "盤前報告", "postclose": "盤後檢討", "weekly": "週報", "tune": "模型調校"}
    PENDING[aid] = {"kind": "run_analysis", "payload": {"mode": args["mode"]}, "created": time.time()}
    return {"pending_action": aid, "summary": f"重新執行：{names.get(args['mode'], args['mode'])}", "status": "等待使用者在畫面上按確認"}


def _data_gaps(a: dict) -> dict | None:
    """哪些資料這次沒取得（例如 FinMind 額度用完）；這些項目在評分中以 0 計，不代表中性。"""
    from src.agents import market_data

    gaps = []
    if not a.get("revenue"):
        gaps.append("月營收")
    if market_data.tw_institutional(a.get("code", "")).empty:
        gaps.append("三大法人買賣超")
    from src.data import providers

    used = {k: providers.source_of(k, a.get("code", "")) for k in ("institutional", "revenue", "valuation", "margin")}
    used = {k: v for k, v in used.items() if v}
    if not gaps:
        return {"missing": [], "sources_used": used} if used else None
    return {"missing": gaps, "sources_used": used,
            "note": "這些資料這次沒有取得（資料來源暫時無法使用或尚無資料），評分時以 0 計；"
                    "這不代表中性，相關分數與排名可能失真，回答時必須以「無法確認：」說明"}


def call_tool(name: str, args: dict) -> dict:
    try:
        if name == "find_stock":
            return {"results": on_demand.resolve(args["query"])}
        if name == "stock_profile":
            return on_demand.profile(args["code"])
        if name == "company_profile":
            from src.agents import mops

            c = mops.company(args["code"])
            if not c.get("error"):
                c["news"] = [{k: n[k] for k in ("date", "title")} for n in c["news"][:8]]
            return c
        if name == "analyze_stock":
            a = on_demand.analyze(args["code"])
            if isinstance(a, dict) and not a.get("error"):
                a = {"data_gaps": _data_gaps(a), "today_status": apidata.today_status(a.get("code", args["code"]), a),
                     **a}  # 放最前面，壓縮時不會被截掉
            return a
        if name == "get_daily_report":
            return apidata.report_summary(args.get("date"))
        if name == "get_portfolio":
            return apidata.portfolio()
        if name == "search_news":
            return {"news": on_demand.search_news(args["query"], 10)}
        if name == "get_macro":
            return apidata.macro_latest()
        if name == "get_rules":
            return rules()
        if name == "get_holding_advice":
            return on_demand.portfolio_plan()
        if name == "get_alerts":
            a = apidata.live_alerts()
            return {k: a[k] for k in ("generated_at", "trading_now", "quotes_date", "counts", "alerts", "limit_up_count",
                                      "limit_down_count", "up_industries", "down_industries", "attention_watch")} | {
                "limit_up_top": a["limit_up"][:10], "limit_down_top": a["limit_down"][:10]}
        if name == "get_data_sources":
            from src.data import providers

            return providers.status()
        if name == "get_potential_picks":
            return apidata.potential()
        if name == "get_model_health":
            return apidata.model_health()
        if name == "simulate_trades":
            return on_demand.simulate(args["trades"])
        if name == "record_trade":
            return _record(args)
        if name == "record_transfer":
            return _transfer(args)
        if name == "get_etf_view":
            return apidata.etf_view()
        if name == "get_futures_quote":
            d = apidata.futures_detail(args["root"])
            d.pop("system_view", None)
            return d
        if name == "get_futures_view":
            return apidata.futures_view()
        if name == "run_analysis":
            return _run(args)
        if name == "import_xq_holdings":
            return xq_sync_preview(args["text"])
        return {"error": f"未知工具 {name}"}
    except Exception as e:  # noqa: BLE001
        log.exception("工具 %s 失敗", name)
        return {"error": f"{name} 執行失敗：{str(e)[:200]}"}


def confirm(aid: str) -> dict:
    from src.api import sync

    act = PENDING.pop(aid, None)
    if not act or time.time() - act["created"] > 1800:
        return {"ok": False, "message": "動作不存在或已過期（30 分鐘），請重新詢問"}
    if act["kind"] == "record_trade":
        return {"ok": True, "message": sync.record_trade(act["payload"])}
    if act["kind"] == "xq_sync":
        rows = sorted(act["payload"], key=lambda r: r["side"] != "SELL")  # 先賣後買
        for r in rows:
            sync.record_trade(r)
        return {"ok": True, "message": f"已依 XQ 庫存寫入 {len(rows)} 筆調整"}
    return {"ok": True, "message": sync.run_workflow(act["payload"]["mode"])}


def cancel(aid: str) -> dict:
    PENDING.pop(aid, None)
    return {"ok": True, "message": "已取消"}


# ---------------------------------------------------------------- LLM 迴圈
def _gemini_loop(messages: list[dict], emit, start: int = 0) -> tuple[str, list]:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    decls = [types.FunctionDeclaration(name=t["name"], description=t["description"], parameters=t["parameters"])
             for t in TOOLS]
    cfg = types.GenerateContentConfig(
        temperature=0, system_instruction=SYSTEM + f"\n今天是 {now_tw().date().isoformat()}。",
        tools=[types.Tool(function_declarations=decls)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
    contents = [types.Content(role="user" if m["role"] == "user" else "model", parts=[types.Part(text=m["content"])])
                for m in messages]
    outputs = []
    last_err = None
    for model in [m for m in settings()["llm"]["gemini_models"] if model_ok(m)]:
        try:
            for _ in range(MAX_STEPS):
                resp = client.models.generate_content(model=model, contents=contents, config=cfg)
                cand = resp.candidates[0].content
                calls = [p.function_call for p in (cand.parts or []) if p.function_call]
                if not calls:
                    return resp.text or "", outputs
                contents.append(cand)
                parts = []
                for fc in calls:
                    args = dict(fc.args or {})
                    emit({"type": "tool", "name": fc.name, "args": args})
                    out = _tag(call_tool(fc.name, args), start + len(outputs) + 1, fc.name)
                    outputs.append({"tool": fc.name, "args": args, "result": out})
                    emit({"type": "tool_result", "name": fc.name, "ok": "error" not in out,
                          "action": out.get("pending_action") and {"id": out["pending_action"], "summary": out["summary"]}})
                    parts.append(types.Part.from_function_response(name=fc.name, response={"result": out}))
                contents.append(types.Content(role="user", parts=parts))
            return "工具呼叫次數過多，請把問題拆小一點再問。", outputs
        except Exception as e:  # noqa: BLE001
            last_err = e
            mark_exhausted(model, str(e))
            log.warning("Gemini %s 失敗：%s", model, str(e)[:200])
            if outputs:
                break
    raise RuntimeError(str(last_err))


GROQ_TOOL_CHARS = 4500  # Groq 免費方案每分鐘 token 上限小，備援時壓縮工具結果與前文
_HEAVY = ("news", "macro_sensitivity", "attribution", "kline", "top_drivers", "feature_ic", "limit_up_top", "limit_down_top")


def _compact(out, budget: int = GROQ_TOOL_CHARS) -> str:
    """去掉大型明細欄位（新聞列表、歸因、敏感度等），仍超過預算就截斷。完整結果仍用於數字核對。"""
    def strip(x, depth=0):
        if isinstance(x, dict):
            return {k: strip(v, depth + 1) for k, v in x.items() if k not in _HEAVY}
        if isinstance(x, list):
            return [strip(v, depth + 1) for v in x[:12]]
        return x

    s = json.dumps(strip(out), ensure_ascii=False, default=str)
    return s if len(s) <= budget else s[:budget] + "…（已截斷）"


def _groq_loop(messages: list[dict], emit, start: int = 0) -> tuple[str, list]:
    from groq import Groq

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    tools = [{"type": "function", "function": t} for t in TOOLS]
    recent = messages[-8:]  # 前文只留最近幾輪，較早的回答截短
    recent = [m if i >= len(recent) - 3 else {**m, "content": m["content"][:600]} for i, m in enumerate(recent)]
    msgs = [{"role": "system", "content": SYSTEM + f"\n今天是 {now_tw().date().isoformat()}。"}] + recent
    outputs = []
    model = settings()["llm"]["groq_models"][0]
    for _ in range(MAX_STEPS):
        resp = client.chat.completions.create(model=model, messages=msgs, tools=tools, temperature=0)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content or "", outputs
        msgs.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            emit({"type": "tool", "name": tc.function.name, "args": args})
            out = _tag(call_tool(tc.function.name, args), start + len(outputs) + 1, tc.function.name)
            outputs.append({"tool": tc.function.name, "args": args, "result": out})
            emit({"type": "tool_result", "name": tc.function.name, "ok": "error" not in out,
                  "action": out.get("pending_action") and {"id": out["pending_action"], "summary": out["summary"]}})
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": _compact(out)})
    return "工具呼叫次數過多，請把問題拆小一點再問。", outputs


def _mark_unverified(text: str, facts) -> tuple[str, list[str]]:
    ok, bad = verify(text, facts)
    if ok:
        return text, []
    nums = fact_numbers(facts)
    from src.agents.verifier import _matches

    def repl(m):
        raw = m.group(0)
        try:
            x = float(raw.replace(",", ""))
        except ValueError:
            return raw
        return raw if (_matches(x, nums) or _matches(abs(x), nums)) else f"{raw}〔未核實〕"

    return NUM_RE.sub(repl, text), bad


def run(messages: list[dict], emit, loop=None) -> dict:
    """執行一輪對話。emit(event) 用來串流進度。回傳最終答案事件。"""
    client = get_client()
    if not client.available():
        return {"type": "answer", "text": "尚未設定 GEMINI_API_KEY / GROQ_API_KEY，AI 助理無法使用。", "verified": False}
    user_text = messages[-1]["content"]
    loops = [loop] if loop else ([_gemini_loop] if os.getenv("GEMINI_API_KEY") else []) + \
        ([_groq_loop] if os.getenv("GROQ_API_KEY") else [])
    text, outputs, err = "", [], None
    for fn in loops:
        try:
            text, outputs = fn(messages, emit)
            break
        except Exception as e:  # noqa: BLE001
            err = e
    if not text and err:
        return {"type": "answer", "text": f"AI 服務暫時無法使用：{str(err)[:120]}", "verified": False}
    # 前文（含先前已核實的回答）也算事實來源，延續對話時引用前面的數字不會被誤判
    facts = {"user": user_text, "tools": [o["result"] for o in outputs], "date": now_tw().date().isoformat(),
             "history": [m["content"] for m in messages[:-1]]}
    ok, bad = verify(_hide_cites(text), facts)
    if not ok and outputs and not loop:
        # 重寫一次：附上未出處數字，要求改用工具結果
        retry = messages + [{"role": "assistant", "content": text},
                            {"role": "user", "content": f"你的回答中 {bad} 這些數字在工具結果中找不到，請重新查詢並只用工具結果的數字重寫，每個論點標註出處；"
                                                        "查不到的就寫「無法確認」（不要再呼叫寫入類工具）。"}]
        try:
            text2, more = (loops[0])(retry, lambda e: None, start=len(outputs))
            facts["tools"] += [o["result"] for o in more]
            outputs = outputs + more
            if verify(_hide_cites(text2), facts)[0]:
                text, ok, bad = text2, True, []
            else:
                text = text2
        except Exception:  # noqa: BLE001
            pass
    text = _style(text)
    text, uncited = _flag_uncited(text) if outputs else (text, 0)
    used_model = any(o["tool"] in MODEL_TOOLS for o in outputs)
    if used_model and re.search(r"預測|機率|預期|預估|命中率", text) and "不是保證" not in text:
        text += "\n\n說明：文中的預測、機率與預期報酬是模型依歷史資料估計的結果，不是保證。"
    text, bad = _mark_unverified(_hide_cites(text), facts)
    text = _show_cites(text)
    text, bad_cites = _check_cites(text, len(outputs))
    sources = []
    for i, o in enumerate(outputs, 1):
        r = o["result"] if isinstance(o["result"], dict) else {}
        sources.append({"id": f"S{i}", "tool": o["tool"], "args": o["args"], "label": SOURCE_DESC.get(o["tool"], o["tool"]),
                        "date": r.get("data_date") or r.get("date") or r.get("asof") or r.get("generated_at"),
                        "links": _links(o["tool"], r), "cited": f"〔S{i}〕" in text})
    actions = [{"id": o["result"]["pending_action"], "summary": o["result"]["summary"]}
               for o in outputs if isinstance(o["result"], dict) and o["result"].get("pending_action")]
    return {"type": "answer", "text": text, "verified": not bad and not bad_cites and not uncited, "unverified": bad + bad_cites + ["未附出處"] * uncited,
            "sources": sources, "actions": actions}


def stream(messages: list[dict]) -> Iterator[str]:
    """SSE：先逐步送出工具進度，最後送出答案。"""
    import queue
    import threading

    q: queue.Queue = queue.Queue()

    def worker():
        try:
            q.put(run(messages, q.put))
        except Exception as e:  # noqa: BLE001
            log.exception("chat 失敗")
            q.put({"type": "answer", "text": f"發生錯誤：{str(e)[:160]}", "verified": False})
        q.put(None)

    threading.Thread(target=worker, daemon=True).start()
    while True:
        ev = q.get()
        if ev is None:
            break
        yield json.dumps(ev, ensure_ascii=False, default=str)


_RATE: dict[str, list[float]] = {}


def rate_ok(sid: str, per_min: int = 8) -> bool:
    now = time.time()
    hist = [t for t in _RATE.get(sid, []) if now - t < 60]
    if len(hist) >= per_min:
        _RATE[sid] = hist
        return False
    _RATE[sid] = hist + [now]
    return True
