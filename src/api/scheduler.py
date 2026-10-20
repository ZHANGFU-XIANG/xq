"""網站端的準點觸發：每 30 秒檢查時刻表（src/schedule.py），到點就以 workflow_dispatch 觸發 invest-agent。

GitHub 的 cron 常延遲數小時，workflow_dispatch 則幾秒內開始。觸發時帶 scheduled=true，
workflow 會再經過 gate，所以重複觸發（例如網站重啟、cron 備援同時到）只會被略過，不會重算兩次。
只在雲端模式啟動；本機模式維持手動執行。
"""
from __future__ import annotations

import logging
import os
import threading
import time

import requests

from src import schedule
from src.config import now_tw

log = logging.getLogger(__name__)
_sent: dict[str, tuple[float, int]] = {}  # 今天已觸發的時段（日期＋key）→ (最後觸發時間, 已觸發次數)
_retry_at: dict[str, float] = {}  # 觸發失敗的時段 5 分鐘後再試
RERUN_AFTER = 15 * 60  # 觸發後 15 分鐘仍未完成（執行失敗）→ 有效時間內再觸發
MAX_TRIES = 3
state: dict = {"last": None, "error": None}


def dispatch(mode: str) -> None:
    repo = os.environ["GITHUB_REPO"]
    r = requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/invest-agent.yml/dispatches",
                      headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"},
                      json={"ref": os.getenv("GITHUB_BRANCH", "main"), "inputs": {"mode": mode, "scheduled": "true"}}, timeout=30)
    r.raise_for_status()


def tick(now=None) -> list[str]:
    now = now or now_tw()
    fired = []
    for key, mode, _start in schedule.due(now):
        tag = f"{now.date()} {key}"
        if schedule.done(key, now.date()) or time.time() < _retry_at.get(tag, 0):
            continue
        last, tries = _sent.get(tag, (0.0, 0))
        if tries and (tries >= MAX_TRIES or time.time() - last < RERUN_AFTER):
            continue
        # 已觸發過但 15 分鐘後仍未完成：多半是執行失敗（例如 10/06 盤前資料缺值），有效時間內重試；
        # 若其實已完成只是還沒同步到網站，workflow 的 gate 會略過，不會重算
        try:
            dispatch(mode)
            _sent[tag] = (time.time(), tries + 1)
            fired.append(key)
            state.update(last={"key": key, "time": now.strftime("%m/%d %H:%M:%S")}, error=None)
            log.info("已觸發排程 %s%s", key, f"（第 {tries + 1} 次，前次未完成）" if tries else "")
        except Exception as e:  # noqa: BLE001
            _retry_at[tag] = time.time() + 300
            state["error"] = f"{now:%m/%d %H:%M} {key}：{str(e)[:160]}"
            log.error("觸發排程失敗 %s：%s", key, e)
    return fired


def _self_ping() -> None:
    """每 6 小時從外部網址造訪自己，降低免費 Space 48 小時無人造訪而休眠的機會（keepalive.yml 已移除，invest-agent 執行時也會順便喚醒）。"""
    host = os.getenv("SPACE_HOST")
    if not host:
        return
    try:
        requests.get(f"https://{host}/api/status", timeout=30)
        state["self_ping"] = now_tw().strftime("%m/%d %H:%M")
    except Exception as e:  # noqa: BLE001
        log.warning("自我喚醒失敗：%s", e)


def start_background(interval: int = 30) -> None:
    from src.api import sync

    def loop():
        for _ in range(60):  # 等第一次與 GitHub 同步完成（拿到 schedule_log）再觸發，避免網站重啟時重複觸發已完成的時段
            if sync.state.get("last_sync"):
                break
            time.sleep(5)
        last_ping = 0.0
        while True:
            if time.time() - last_ping > 6 * 3600:
                last_ping = time.time()
                _self_ping()
            try:
                tick()
            except Exception:  # noqa: BLE001
                log.exception("排程檢查失敗")
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True).start()
