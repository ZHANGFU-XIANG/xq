"""EngineerAgent（SRE）：網站背景每 5 分鐘自我檢查，能安全修復的自動修，修不好的寄信並附上搜尋到的可能原因。

檢查項目：
1. GitHub Actions：invest-agent／deploy-space／send-email 最近一次是否失敗（之後沒有成功的）
2. 排程漏跑：今天已過有效時間、卻沒有完成紀錄的時段（schedule_log）
3. 網站預載是否失敗、與 GitHub 同步是否出錯
4. DataGuard 最近一次是否判定資料不完整
5. LLM 供應商是否全部不可用（只影響文字，數字仍由程式算）
自動修復（只做安全、可重複的動作，不改程式碼、不動交易紀錄）：
- 預載失敗 → 重新預載（最多 3 次）
- 資料不完整 → 清掉記憶體中的市場資料，下一次請求重新取得（每 30 分鐘最多一次）
- 排程漏跑且仍在有效時間內 → 交給 scheduler 重新觸發（scheduler 已有重試，這裡只記錄）
通報：新的「嚴重」事件寄 Email（同一事件一天一次），附 SearchAgent 找到的網頁。
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time

import requests

from src.config import now_tw

log = logging.getLogger(__name__)
EVENTS: list[dict] = []
STATE: dict = {"last": None, "checks": {}, "error": None}
_sent: set[str] = set()
_fix_at: dict[str, float] = {}
_warm_retries = [0]
WORKFLOWS = ("invest-agent", "deploy-space", "send-email")


def _event(key: str, level: str, title: str, detail: str = "", fixed: str = "") -> dict:
    e = {"time": now_tw().strftime("%m/%d %H:%M"), "key": key, "level": level, "title": title, "detail": detail[:400],
         "fixed": fixed}
    if not any(x["key"] == key and x["time"][:5] == e["time"][:5] for x in EVENTS[-30:]):
        EVENTS.append(e)
        del EVENTS[:-60]
    return e


def check_actions() -> list[dict]:
    repo, tok = os.getenv("GITHUB_REPO"), os.getenv("GITHUB_TOKEN")
    if not (repo and tok):
        return []
    r = requests.get(f"https://api.github.com/repos/{repo}/actions/runs?per_page=30",
                     headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}, timeout=30)
    r.raise_for_status()
    out, seen = [], set()
    for run in r.json().get("workflow_runs", []):  # 新到舊：每個 workflow 只看最近一次完成的
        name = run.get("name")
        if name not in WORKFLOWS or name in seen or run.get("status") != "completed":
            continue
        seen.add(name)
        if run.get("conclusion") == "failure":
            out.append({"key": f"gha-{name}-{run['id']}", "level": "嚴重", "title": f"GitHub Actions {name} 最近一次執行失敗",
                        "detail": f"{run.get('display_title', '')}（{run.get('updated_at', '')}）{run.get('html_url', '')}"})
    return out


def check_schedule(now: dt.datetime | None = None) -> list[dict]:
    from src import schedule

    now = now or now_tw()
    out = []
    for key, mode, start, end in schedule._entries(now.date()):
        if end <= now and not schedule.done(key, now.date()) and mode != "intraday":
            out.append({"key": f"missed-{now.date()}-{key}", "level": "嚴重", "title": f"排程 {key} 今天沒有完成",
                        "detail": f"有效時間 {start:%H:%M}～{end:%H:%M} 已過；請到 GitHub Actions 手動執行 {mode}"})
    return out


def run_checks(warm: dict | None = None, rewarm=None) -> list[dict]:
    from src.agents import data_guard
    from src.api import sync

    found: list[dict] = []
    for name, fn in (("actions", check_actions), ("schedule", check_schedule)):
        try:
            found += fn()
            STATE["checks"][name] = "ok"
        except Exception as e:  # noqa: BLE001
            STATE["checks"][name] = f"失敗：{str(e)[:80]}"
    if warm and warm.get("error"):
        fixed = ""
        if rewarm and _warm_retries[0] < 3:
            _warm_retries[0] += 1
            threading.Thread(target=rewarm, daemon=True).start()
            fixed = f"已自動重新預載（第 {_warm_retries[0]} 次）"
        found.append({"key": "warm", "level": "嚴重", "title": "網站預載失敗", "detail": warm["error"], "fixed": fixed})
    if sync.state.get("error"):
        found.append({"key": "sync", "level": "警告", "title": "與 GitHub 同步失敗", "detail": str(sync.state["error"])})
    g = data_guard.STATE.get("last")
    if g and g.get("level") == "severe":
        fixed = ""
        if time.time() - _fix_at.get("ctx", 0) > 1800:
            from src import pipeline

            pipeline._CTX.clear()  # 下一次請求重新取得市場資料
            _fix_at["ctx"] = time.time()
            fixed = "已清除記憶體中的市場資料，下一次請求重新取得"
        found.append({"key": f"data-{g.get('latest')}", "level": "嚴重", "title": "資料不完整",
                      "detail": "；".join(i["msg"] for i in g.get("issues", [])), "fixed": fixed})
    try:
        from src.llm.client import get_client

        if not get_client().available():
            found.append({"key": "llm", "level": "警告", "title": "LLM 全部不可用（文字改用模板，數字不受影響）"})
    except Exception:  # noqa: BLE001
        log.warning("LLM 可用性檢查失敗", exc_info=True)
    return found


def tick(warm: dict | None = None, rewarm=None) -> int:
    from src.agents import search_agent
    from src.reports import emailer

    found = run_checks(warm, rewarm)
    STATE["last"] = now_tw().strftime("%m/%d %H:%M:%S")
    day = now_tw().date().isoformat()
    new = []
    for f in found:
        e = _event(f["key"], f["level"], f["title"], f.get("detail", ""), f.get("fixed", ""))
        if f["level"] == "嚴重" and f"{day}|{f['key']}" not in _sent:
            refs = search_agent.explain_error(f.get("detail") or f["title"]) if "失敗" in f["title"] else []
            e["refs"] = refs
            new.append(e)
    if not new:
        return 0
    body = "\n\n".join(
        f"■ ［嚴重］{e['title']}\n{e['detail']}" + (f"\n自動處理：{e['fixed']}" if e["fixed"] else "")
        + "".join(f"\n- 可能原因：{r['title']} {r['url']}" for r in e.get("refs", [])[:3]) for e in new)
    if emailer.send(f"[投資競賽] 系統異常 {len(new)} 項：{new[0]['title']}", body + "\n\n系統自動檢查・詳情請開網站「模型 → 系統健康」", []):
        for e in new:
            _sent.add(f"{day}|{e['key']}")
    return len(new)


def start_background(warm: dict, rewarm, interval: int = 300) -> None:
    def loop():
        time.sleep(300)  # 等預載與同步
        while True:
            try:
                tick(warm, rewarm)
                STATE["error"] = None
            except Exception as e:  # noqa: BLE001
                STATE["error"] = str(e)[:160]
                log.exception("SRE 檢查失敗")
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True).start()


def status() -> dict:
    return {"last": STATE["last"], "checks": STATE["checks"], "error": STATE["error"], "events": EVENTS[::-1][:30]}
