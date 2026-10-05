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
_sent: set[str] = set()  # 今天已觸發的時段（日期＋key）
_retry_at: dict[str, float] = {}  # 觸發失敗的時段 5 分鐘後再試
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
        if tag in _sent or schedule.done(key, now.date()) or time.time() < _retry_at.get(tag, 0):
            continue
        try:
            dispatch(mode)
            _sent.add(tag)
            fired.append(key)
            state.update(last={"key": key, "time": now.strftime("%m/%d %H:%M:%S")}, error=None)
            log.info("已觸發排程 %s", key)
        except Exception as e:  # noqa: BLE001
            _retry_at[tag] = time.time() + 300
            state["error"] = f"{now:%m/%d %H:%M} {key}：{str(e)[:160]}"
            log.error("觸發排程失敗 %s：%s", key, e)
    return fired


def start_background(interval: int = 30) -> None:
    from src.api import sync

    def loop():
        for _ in range(60):  # 等第一次與 GitHub 同步完成（拿到 schedule_log）再觸發，避免網站重啟時重複觸發已完成的時段
            if sync.state.get("last_sync"):
                break
            time.sleep(5)
        while True:
            try:
                tick()
            except Exception:  # noqa: BLE001
                log.exception("排程檢查失敗")
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True).start()
