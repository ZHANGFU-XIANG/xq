"""ReportWriterAgent：把程式算好的事實寫成白話分析。

流程：LLM 撰寫 → Verifier 核對每個數字 → 不通過則附錯誤重寫（最多 2 次）→ 仍失敗改用模板文字。
沒有 API key 時直接用模板；所有細部理由本來就由程式產生，報告仍完整。
"""
from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

from src.agents.verifier import verify
from src.llm.client import LLMError, get_client

log = logging.getLogger(__name__)

SYSTEM = (
    "你是台股投資競賽隊伍的首席研究員，使用繁體中文與台灣用語。只能使用提供的 JSON 事實撰寫，"
    "禁止加入任何 JSON 裡沒有的數字、價格、日期、公司或事件；數字必須照抄。分析要具體、有因果、可執行，不誇大。"
    "不要在文字中出現 JSON 欄位名稱或英文變數名稱（例如 gap_hit_rate），百分比請寫成 80.0% 而非 0.8。"
)


class OrderNote(BaseModel):
    code: str
    note: str = Field(description="2-3 句白話總結：為何選、為何這個張數、怎麼出場")


class WriterOut(BaseModel):
    market_summary: str = Field(description="美股隔夜、國際與新聞重點，3-5 句")
    outlook: str = Field(description="今日台股開盤與盤中走向判斷，2-4 句，需提及模型命中率")
    allocation_analysis: str = Field(description="今日進攻籃與穩健籃的比例、調整原因、兩籃各買了什麼與為何，3-6 句")
    daily_move_analysis: str = Field(description="持股（或候選股）最近交易日為何漲／跌：大盤、產業、個股因素各貢獻多少，4-8 句")
    recovery_plan: str = Field(description="虧損如何補回：續抱/加碼/換股/停損的決策與理由、換到哪一檔與原因，4-8 句；無虧損則說明維持獲利的做法")
    macro_analysis: str = Field(description="國際大事對手中股票的影響方向、幅度（金額）、後續決策，逐項列舉，4-8 句")
    order_notes: list[OrderNote]


class PostMortem(BaseModel):
    summary: str = Field(description="失誤原因分析與修正內容，4-8 句")


def _fmt_list(items, fn, empty):
    return "；".join(fn(x) for x in items) if items else empty


def template_text(facts: dict) -> dict:
    us = {r["ticker"]: r for r in facts["us_snapshot"]}
    parts = [f"{us[t]['name']} {us[t]['change_pct']:+.2f}%" for t in ("^SOX", "^IXIC", "^GSPC", "TSM", "^TNX") if t in us]
    summary = "美股前一交易日：" + "、".join(parts) + "。"
    if facts.get("market_view"):
        summary += "新聞面：" + facts["market_view"]
    tx = facts["predictions"].get("TAIEX")
    outlook = "大盤預測資料不足。"
    if tx:
        outlook = (f"模型預測加權指數開盤 {tx['gap_pct']:+.2f}%（{tx['direction']}），開盤後 {tx['intraday_pct']:+.2f}%"
                   f"（{tx['after_open']}）；樣本外方向命中率 {tx['gap_hit_rate'] * 100:.1f}%，誤差區間約 ±{tx['gap_band_pct']:.2f}%。")
    att = facts.get("attribution") or {}
    daily = _fmt_list(att.get("rows", []), lambda r: f"{r['code']} {r['name']} {r['verdict']}", "尚無持股可歸因。")
    if att and att.get("taiex_ret_pct") is not None:
        daily = f"{att['date']} 加權指數 {att['taiex_ret_pct']:+.2f}%。" + daily
    rec = facts.get("recovery") or {}
    recovery = "；".join(rec.get("summary", [])) + "。" + _fmt_list(
        rec.get("items", []), lambda i: f"{i['code']} {i['name']}：{i['decision']}（{i['why']}）", "")
    macro = _fmt_list(facts.get("macro", []), lambda e: (
        f"{e['event']} → {e['factor_name']}{'上升' if e['direction'] > 0 else '下降'}，"
        f"持股合計預估影響 {e['total_amount']:+,} 元（總資產 {e['total_pct_nav']:+.2f}%）"), "今日未辨識出重大國際事件。")
    notes = []
    for o in facts["orders"]:
        if o["action"] == "BUY":
            notes.append({"code": o["code"], "note": f"買進 {o['name']} {o['shares']:,} 股，限價 {o['limit_price']}，停損 {o['stop']}。"})
        else:
            notes.append({"code": o["code"], "note": f"賣出 {o['name']} {o['shares']:,} 股：{o['why_sell']}。"})
    al = facts.get("allocation") or {}
    alloc_txt = "；".join(al.get("reasons", [])) + "。" if al else ""
    for b, name in (("offense", "進攻籃"), ("defense", "穩健籃")):
        buys = [f"{o['code']} {o['name']}" for o in facts["orders"] if o["action"] == "BUY" and o.get("basket") == b]
        if buys:
            alloc_txt += f"{name}買進：{'、'.join(buys)}。"
    return {"market_summary": summary, "outlook": outlook, "allocation_analysis": alloc_txt,
            "daily_move_analysis": daily, "recovery_plan": recovery,
            "macro_analysis": macro, "order_notes": notes, "source": "template"}


def _slim(facts: dict) -> dict:
    keep = ("date", "us_snapshot", "market_view", "events", "attribution", "recovery", "macro", "compliance", "portfolio",
            "allocation")
    slim = {k: facts.get(k) for k in keep}
    slim["orders"] = [{k: o.get(k) for k in ("action", "code", "name", "basket_name", "shares", "lots", "odd", "limit_price", "stop",
                                             "why_stock", "why_qty", "why_more", "why_less", "why_sell", "exit_plan")}
                      for o in facts["orders"]]
    slim["predictions"] = {k: v for k, v in facts["predictions"].items()
                           if k == "TAIEX" or any(o["code"] == k for o in facts["orders"])}
    slim["top_picks"] = facts.get("top_picks", [])[:10]
    slim["def_picks"] = facts.get("def_picks", [])[:8]
    return slim


def write(facts: dict) -> dict:
    client = get_client()
    if not client.available():
        return template_text(facts)
    prompt = "事實資料(JSON)：\n" + json.dumps(_slim(facts), ensure_ascii=False, default=str)
    feedback = ""
    for attempt in range(3):
        try:
            out = client.structured(prompt + feedback, WriterOut, system=SYSTEM)
        except LLMError as e:
            log.warning("撰稿失敗，改用模板：%s", e)
            return template_text(facts)
        text = "\n".join([out.market_summary, out.outlook, out.allocation_analysis, out.daily_move_analysis, out.recovery_plan,
                          out.macro_analysis] + [n.note for n in out.order_notes])
        ok, bad = verify(text, facts)
        valid_codes = {o["code"] for o in facts["orders"]}
        bad_codes = [n.code for n in out.order_notes if n.code not in valid_codes]
        if ok and not bad_codes:
            res = out.model_dump()
            res["source"] = "llm-verified"
            return res
        log.warning("Verifier 退回（第 %d 次）：數字 %s 代號 %s", attempt + 1, bad, bad_codes)
        feedback = f"\n\n上一版包含事實資料中不存在的數字 {bad} 或代號 {bad_codes}，請刪除或改成資料中的原數字後重寫。"
    return template_text(facts)


def postmortem(facts: dict) -> str:
    """調整 Agent 的失誤檢討：只用程式產生的範本文字。
    10/09 LLM 寫出「進攻籃報酬低於預期」卻又提高進攻比例的矛盾說明；數字核對抓不到因果錯誤，所以調參說明不經 LLM。"""
    base = "；".join(i["detail"] for i in facts["issues"]) or "無重大失誤"
    changes = "；".join(f"{c['param']}：{c.get('reason', '')}（回測指標 {c.get('metric_before', '—')} → {c.get('metric_after', '—')}）"
                        for c in facts["changes"]) or "未修改參數"
    return f"偵測結果：{base}。修正：{changes}。"
