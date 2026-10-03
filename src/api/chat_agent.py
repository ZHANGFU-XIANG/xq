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

SYSTEM = """你是「2026 德明X富達投資擂台」參賽隊伍的 AI 投資助理，使用繁體中文與台灣用語。
規則：
1. 回答任何股價、張數、金額、比例、機率等數字前，必須先呼叫工具取得；只能使用工具結果中的數字，禁止自行估算或憑記憶。
2. 使用者問到任何股票（包含每日建議沒有的），先用 find_stock 找代號，再用 analyze_stock 取得分析與操作建議。
2b. 問公司在做什麼、財報、EPS、營收、股利、重大訊息時，用 company_profile（公開資訊觀測站官方資料）。
3. 給買進建議時，要說明：屬於進攻籃或穩健籃與原因、建議股數（張＋零股）、限價、停損、出場計畫、主要風險。
4. 使用者說已成交（例如「我買了 2330 一張 1000 元」）時呼叫 record_trade；要重新產生報告時呼叫 run_analysis。這兩個工具只會建立待確認動作，請提醒使用者在畫面上按「確認」。
5. 「如果買/賣…會怎樣」用 simulate_trades 試算，說明成本、持股率與競賽規則影響。
5b. 問到警示、漲跌停、負面新聞、要不要賣、該賣多少時，先呼叫 get_alerts，照工具給的建議股數與理由回答。
6. 競賽重點：持股率需 ≥70%（每日收盤檢查）、至少 5 檔、10 筆交易、禁止權證、只有正報酬才排名。
7. 註明資料日期；不確定或工具沒有的資料就直說沒有。回答精簡、條列、可執行。不要輸出 JSON 或英文欄位名稱。"""

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
         "code": {"type": "string"}, "side": {"type": "string", "enum": ["BUY", "SELL"]}, "shares": {"type": "integer"},
         "price": {"type": "number"}, "margin": {"type": "boolean"}, "date": {"type": "string", "description": "YYYY-MM-DD，預設今天"}},
         "required": ["code", "side", "shares", "price"]}},
    {"name": "run_analysis", "description": "重新執行分析並產生報告（建立待確認動作）",
     "parameters": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["premarket", "postclose", "weekly", "tune"]}},
                    "required": ["mode"]}},
]


# ---------------------------------------------------------------- 工具實作
def _record(args: dict) -> dict:
    from src.agents.compliance import is_warrant

    code = str(args["code"]).strip()
    if is_warrant(code):
        return {"error": "權證不可交易（競賽規則）"}
    row = {"date": args.get("date") or now_tw().date().isoformat(), "code": code, "side": args["side"].upper(),
           "shares": int(args["shares"]), "price": float(args["price"]), "margin": int(bool(args.get("margin"))),
           "note": "AI 助理記錄"}
    name = on_demand.stock_meta(code)["name"]
    aid = uuid.uuid4().hex[:10]
    PENDING[aid] = {"kind": "record_trade", "payload": row, "created": time.time()}
    return {"pending_action": aid, "summary": f"記錄成交：{row['date']} {'買進' if row['side'] == 'BUY' else '賣出'} "
            f"{code} {name} {row['shares']:,} 股 @ {row['price']}" + ("（融資）" if row["margin"] else ""),
            "status": "等待使用者在畫面上按確認"}


def _run(args: dict) -> dict:
    aid = uuid.uuid4().hex[:10]
    names = {"premarket": "盤前報告", "postclose": "盤後檢討", "weekly": "週報", "tune": "模型調校"}
    PENDING[aid] = {"kind": "run_analysis", "payload": {"mode": args["mode"]}, "created": time.time()}
    return {"pending_action": aid, "summary": f"重新執行：{names.get(args['mode'], args['mode'])}", "status": "等待使用者在畫面上按確認"}


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
            return on_demand.analyze(args["code"])
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
        if name == "get_alerts":
            a = apidata.live_alerts()
            return {k: a[k] for k in ("generated_at", "trading_now", "quotes_date", "counts", "alerts", "limit_up_count",
                                      "limit_down_count", "up_industries", "down_industries", "attention_watch")} | {
                "limit_up_top": a["limit_up"][:10], "limit_down_top": a["limit_down"][:10]}
        if name == "get_model_health":
            return apidata.model_health()
        if name == "simulate_trades":
            return on_demand.simulate(args["trades"])
        if name == "record_trade":
            return _record(args)
        if name == "run_analysis":
            return _run(args)
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
    return {"ok": True, "message": sync.run_workflow(act["payload"]["mode"])}


def cancel(aid: str) -> dict:
    PENDING.pop(aid, None)
    return {"ok": True, "message": "已取消"}


# ---------------------------------------------------------------- LLM 迴圈
def _gemini_loop(messages: list[dict], emit) -> tuple[str, list]:
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
                    out = call_tool(fc.name, args)
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


def _groq_loop(messages: list[dict], emit) -> tuple[str, list]:
    from groq import Groq

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    tools = [{"type": "function", "function": t} for t in TOOLS]
    msgs = [{"role": "system", "content": SYSTEM + f"\n今天是 {now_tw().date().isoformat()}。"}] + messages
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
            out = call_tool(tc.function.name, args)
            outputs.append({"tool": tc.function.name, "args": args, "result": out})
            emit({"type": "tool_result", "name": tc.function.name, "ok": "error" not in out,
                  "action": out.get("pending_action") and {"id": out["pending_action"], "summary": out["summary"]}})
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(out, ensure_ascii=False, default=str)})
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
    facts = {"user": user_text, "tools": [o["result"] for o in outputs], "date": now_tw().date().isoformat()}
    ok, bad = verify(text, facts)
    if not ok and outputs and not loop:
        # 重寫一次：附上未出處數字，要求改用工具結果
        retry = messages + [{"role": "assistant", "content": text},
                            {"role": "user", "content": f"你的回答中 {bad} 這些數字在工具結果中找不到，請只用工具結果的數字重寫（不要再呼叫寫入類工具）。"}]
        try:
            text2, more = (loops[0])(retry, lambda e: None)
            facts["tools"] += [o["result"] for o in more]
            if verify(text2, facts)[0]:
                text, ok, bad = text2, True, []
            else:
                text = text2
        except Exception:  # noqa: BLE001
            pass
    text, bad = _mark_unverified(text, facts)
    sources = []
    for o in outputs:
        r = o["result"] if isinstance(o["result"], dict) else {}
        sources.append({"tool": o["tool"], "args": o["args"],
                        "date": r.get("data_date") or r.get("date") or r.get("asof")})
    actions = [{"id": o["result"]["pending_action"], "summary": o["result"]["summary"]}
               for o in outputs if isinstance(o["result"], dict) and o["result"].get("pending_action")]
    return {"type": "answer", "text": text, "verified": not bad, "unverified": bad, "sources": sources, "actions": actions}


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
