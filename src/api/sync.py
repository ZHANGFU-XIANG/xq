"""雲端模式的資料同步：GitHub repo 是唯一真實來源（GitHub Actions 每日寫入報告與狀態）。

- 讀：啟動時與每 10 分鐘 clone/pull 私有 repo，把 data/、reports/、config/learned_params.yaml 複製到本機工作目錄
- 寫：記錄成交 → GitHub Contents API commit data/trades.csv
- 重跑分析 → 觸發 invest-agent.yml 的 workflow_dispatch
本機模式（APP_MODE=local 或未設 GITHUB_REPO）直接讀寫專案資料夾。
"""
from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

from src.config import DATA_DIR
from src.state import portfolio as pfm

log = logging.getLogger(__name__)
REPO_DIR = Path(os.getenv("REPO_CACHE_DIR", "/tmp/invest-repo"))
SYNC_PATHS = ["data/facts", "data/intraday", "data/trades.csv", "data/position_meta.json", "data/nav_history.json",
              "data/pending_orders.json", "data/prediction_log.csv", "data/tuning_log.json",
              "config/learned_params.yaml", "reports"]
_lock = threading.Lock()
state = {"last_sync": None, "error": None}


def cloud() -> bool:
    return os.getenv("APP_MODE", "local") == "cloud" and bool(os.getenv("GITHUB_REPO"))


def _gh_headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}


def pull() -> None:
    if not cloud():
        return
    with _lock:
        try:
            url = f"https://x-access-token:{os.environ['GITHUB_TOKEN']}@github.com/{os.environ['GITHUB_REPO']}.git"
            if (REPO_DIR / ".git").exists():
                subprocess.run(["git", "-C", str(REPO_DIR), "pull", "--ff-only", "-q"], check=True, timeout=120)
            else:
                subprocess.run(["git", "clone", "--depth", "1", "-q", url, str(REPO_DIR)], check=True, timeout=300)
            root = DATA_DIR.parent
            for rel in SYNC_PATHS:
                src, dst = REPO_DIR / rel, root / rel
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                elif src.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            state.update(last_sync=time.time(), error=None)
        except Exception as e:  # noqa: BLE001
            state["error"] = str(e)[:200]
            log.error("同步失敗：%s", e)


def start_background(interval: int = 600) -> None:
    def loop():
        while True:
            pull()
            time.sleep(interval)

    if cloud():
        threading.Thread(target=loop, daemon=True).start()


def record_trade(row: dict) -> str:
    """寫入一筆成交。雲端：commit 到 GitHub；本機：寫入 data/trades.csv。回傳說明文字。"""
    return record_trades([row])


def record_trades(rows: list[dict], message: str | None = None) -> str:
    """寫入多筆成交（雲端合併成一次 commit）。"""
    pfm.build_portfolio(pfm.load_trades() + [{k: str(v) for k, v in r.items()} for r in rows])  # 先驗證（例如賣超過持有會丟錯）
    if not cloud():
        pfm.append_trades(rows)
        return "已寫入本機 data/trades.csv"
    repo = os.environ["GITHUB_REPO"]
    api = f"https://api.github.com/repos/{repo}/contents/data/trades.csv"
    r = requests.get(api, headers=_gh_headers(), timeout=30)
    r.raise_for_status()
    cur = base64.b64decode(r.json()["content"]).decode("utf-8")
    if not cur.endswith("\n"):
        cur += "\n"
    lines = cur.splitlines()
    if lines and lines[0].lstrip("﻿").split(",") != pfm.TRADE_FIELDS:  # 舊表頭：補上新欄位
        n_old = len(lines[0].split(","))
        pad = "," * (len(pfm.TRADE_FIELDS) - n_old)
        cur = "\n".join([",".join(pfm.TRADE_FIELDS)] + [ln + pad for ln in lines[1:] if ln.strip()]) + "\n"
    new = "".join(",".join(str(row.get(k, "")) for k in pfm.TRADE_FIELDS) + "\n" for row in rows)
    if message is None:
        message = (f"record trade {rows[0]['date']} {rows[0]['side']} {rows[0]['code']} {rows[0]['shares']}@{rows[0]['price']}"
                   if len(rows) == 1 else f"record {len(rows)} trades {rows[0]['date']}")
    body = {"message": message, "content": base64.b64encode((cur + new).encode("utf-8")).decode(), "sha": r.json()["sha"]}
    requests.put(api, headers=_gh_headers(), json=body, timeout=30).raise_for_status()
    pfm.append_trades(rows)  # 本機副本立即反映，下次 pull 會與 repo 一致
    return f"已提交到 GitHub（{repo} data/trades.csv）"


def run_workflow(mode: str) -> str:
    if not cloud():
        subprocess.Popen([sys.executable, "-m", "src.main", mode, "--force"], cwd=str(DATA_DIR.parent))
        return f"已在本機背景執行 {mode}，完成後重新整理即可看到新報告"
    repo = os.environ["GITHUB_REPO"]
    r = requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/invest-agent.yml/dispatches",
                      headers=_gh_headers(), json={"ref": os.getenv("GITHUB_BRANCH", "main"),
                                                   "inputs": {"mode": mode, "force": "true"}}, timeout=30)
    r.raise_for_status()
    return f"已觸發 GitHub Actions 執行 {mode}（約 5～10 分鐘），完成後會自動同步並寄出報告"
