"""BriefingAgent：把各 Agent 的結果統整成一份「總報告」（網站一頁＋HTML Email），數字全部來自各 Agent 的程式計算。

章節：資料檢查 → 風險（追繳、壓力、黑天鵝）→ 交易日曆 → 夜盤與國際 → 產業新聞 → 假消息過濾 → 論壇風向 → 持股多空辯論
寄送：網站背景每天 06:40（開盤前）與 21:30（美股開盤後）各一封；盤前報告也附上同一份內容。
"""
from __future__ import annotations

import logging

from src.config import now_tw

log = logging.getLogger(__name__)
LAST: dict = {"v": None}


def _fmt(n) -> str:
    return f"{n:+,.0f}" if isinstance(n, (int, float)) else "—"


def build(res: dict[str, dict]) -> dict:
    """res：registry.run 的結果 {name: {ok, output, error}}。"""
    o = {k: (v.get("output") or {}) for k, v in res.items()}
    sec: list[dict] = []
    g = o.get("data_guard") or {}
    if g.get("level") in ("severe", "corrected") and g.get("issues"):
        sec.append({"title": "資料檢查", "level": "嚴重" if g.get("level") == "severe" else "注意",
                    "lines": [i["msg"] for i in g.get("issues", [])]})
    rk = o.get("risk") or {}
    if rk.get("stress"):
        lines = [f"［{a['level']}］{a['title']}：{a['body']}" for a in rk.get("alerts", [])]
        s5 = next((s for s in rk["stress"] if s["scenario"] == "加權指數 −5%"), None)
        if s5:
            lines.append(f"壓力測試：加權 −5% 估計 {_fmt(s5['total'])} 元（{s5['pct_nav']}%），其中期貨 {_fmt(s5['futures'])}")
        v = (rk.get("var") or {})
        if v.get("end"):
            lines.append(f"到 11/27 收正機率 {v['end']['p_positive'] * 100:.0f}%（部位不變的模擬，中位 {v['end']['median']:+.1f}%）")
        sec.append({"title": "風險", "level": "嚴重" if any(a["level"] == "嚴重" for a in rk.get("alerts", [])) else "注意",
                    "lines": lines})
    cal = o.get("calendar") or {}
    if cal.get("events"):
        sec.append({"title": "未來 5 個交易日注意事項", "level": None,
                    "lines": [f"{e['date'][5:]} {e['title']}：{e['note']}" for e in cal["events"]]})
    ov = o.get("overnight") or {}
    gl = o.get("global") or {}
    lines = []
    if ov.get("tx_night"):
        t = ov["tx_night"]
        lines.append(f"台指期夜盤 {t['price']:,.0f}（{t['chg_pct']:+.2f}%，{t['change']:+,.0f} 點），估計持股＋期貨開盤影響 "
                     f"{_fmt((ov.get('impact') or {}).get('total'))} 元")
    lines += gl.get("highlights", [])
    if (gl.get("impact") or {}).get("total") is not None:
        lines.append(f"國際因子對持股的估計影響 {_fmt(gl['impact']['total'])} 元（{gl['impact'].get('basis', '')}）")
    if lines:
        sec.append({"title": "夜盤與國際市場", "level": None, "lines": lines})
    ind = o.get("industry") or {}
    lines = []
    for s, v in (ind.get("sectors") or {}).items():
        if v["pos"] or v["neg"]:
            top = v["items"][0] if v["items"] else None
            lines.append(f"{s}（{'、'.join(v['names'][:3])}）：利多 {v['pos']}、利空 {v['neg']}"
                         + (f"；{top['title'][:40]}［{top['fc_label']}］" if top else ""))
    if lines:
        sec.append({"title": "產業新聞", "level": None, "lines": lines})
    fc = o.get("fact_check") or {}
    if fc.get("flagged"):
        sec.append({"title": "未證實或已澄清的消息（不列入判斷）", "level": None,
                    "lines": [f"{x['title'][:50]}（{'、'.join(x['fc_notes']) or x['fc_label']}）" for x in fc["flagged"][:6]]})
    fo = o.get("forum") or {}
    if fo.get("hot"):
        sec.append({"title": f"PTT 論壇風向（{fo.get('label', '僅資訊')}）", "level": None,
                    "lines": [f"{s['code']} {s['name']}：提及 {s['mentions']} 次、推文 {s['push']}、多空 {s['direction']:+.1f}"
                              for s in fo["hot"][:6]]})
    db = o.get("debate") or {}
    if db.get("items"):
        sec.append({"title": "持股多空辯論（結論以系統持股建議為準）", "level": None,
                    "lines": [f"{d['code']} {d['name']}［{d.get('verdict') or '推薦'}／論點{d['lean']}］{d['key_dispute'][:80]}"
                              for d in db["items"][:10]]})
    failed = [f"{k}：{v.get('error')}" for k, v in res.items() if not v.get("ok")]
    text = "\n\n".join(f"{s['title']}：\n" + "\n".join(f"- {x}" for x in s["lines"]) for s in sec if s["lines"])
    out = {"time": now_tw().strftime("%Y-%m-%d %H:%M"), "sections": sec, "text": text, "failed": failed,
           "agents": {k: {"ok": v.get("ok"), "seconds": v.get("seconds")} for k, v in res.items()}}
    LAST["v"] = out
    return out
