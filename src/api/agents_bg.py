"""網站背景：每小時跑一次全部輔助 Agent（經 registry）並統整成總報告；每天 06:40、21:30 寄出總報告 Email。

- 盤中（09:00～13:35）只跑不含 LLM 的快速 Agent（每 30 分鐘），避免和盤中建議搶資源
- 寄信：06:40（下一個交易日開盤前）與 21:30（美股開盤後），同一時段一天只寄一次
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time

from src.config import is_trading_day, now_tw

log = logging.getLogger(__name__)
STATE: dict = {"last_run": None, "running": False, "error": None, "sent": set()}
SEND_AT = ((6, 40), (21, 30))


def _picks() -> list[str]:
    try:
        from src.api import data

        return [p["code"] for p in data.high_return_picks(limit=20).get("items", [])]
    except Exception as e:  # noqa: BLE001
        log.info("總報告：取綜合排名失敗 %s", e)
        return []


def refresh(names: list[str] | None = None) -> dict:
    from src.agents import agents_catalog, briefing, registry

    if STATE["running"]:
        return briefing.LAST["v"] or {}
    STATE["running"] = True
    try:
        res = registry.run(names or agents_catalog.ALL, picks=_picks())
        out = briefing.build(res)
        STATE.update(last_run=time.time(), error=None)
        return out
    except Exception as e:  # noqa: BLE001
        STATE["error"] = str(e)[:160]
        log.exception("總報告更新失敗")
        return {}
    finally:
        STATE["running"] = False


def _due_send(now) -> str | None:
    for h, m in SEND_AT:
        tag = f"{now.date()} {h:02d}:{m:02d}"
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if 0 <= (now - start).total_seconds() < 20 * 60 and tag not in STATE["sent"]:
            # 06:40 只在交易日寄；21:30 在「今天或明天是交易日」的晚上寄
            tomorrow = now.date() + dt.timedelta(days=1)
            if (h < 12 and is_trading_day(now.date())) or (h >= 12 and (is_trading_day(now.date()) or is_trading_day(tomorrow))):
                return tag
    return None


def _log_forum(now) -> None:
    """PTT 封鎖 GitHub Actions 的 IP（403），論壇熱度改由網站每天收盤後（14:00 起）記錄一次並寫回 GitHub。"""
    tag = f"forum {now.date()}"
    if tag in STATE["sent"] or now.hour < 14 or not is_trading_day(now.date()):
        return
    from src.agents import forum_sentiment
    from src.api import sync

    forum_sentiment._CACHE.clear()
    forum_sentiment.run(_picks(), write_log=True)
    sync.put_file("data/forum_log.csv", forum_sentiment.LOG.read_text(encoding="utf-8"), f"forum log {now.date()}")
    STATE["sent"].add(tag)


def tick() -> None:
    from src.agents import agents_catalog, alerts, briefing
    from src.reports import emailer

    now = now_tw()
    trading = alerts.is_trading_hours()
    gap = 30 * 60 if trading else 60 * 60
    if not STATE["last_run"] or time.time() - STATE["last_run"] > gap:
        refresh(agents_catalog.FAST if trading else agents_catalog.ALL)
    try:
        _log_forum(now)
    except Exception as e:  # noqa: BLE001
        log.warning("論壇紀錄失敗：%s", e)
    tag = _due_send(now)
    if tag:
        b = briefing.LAST["v"] if STATE["last_run"] and time.time() - STATE["last_run"] < 40 * 60 else refresh()
        if b and b.get("text"):
            title = "開盤前總報告" if now.hour < 12 else "晚間總報告（美股開盤後）"
            if emailer.send(f"[投資競賽] {title} {now:%m/%d %H:%M}", f"{title}\n\n{b['text']}", []):
                STATE["sent"].add(tag)


def start_background(interval: int = 60) -> None:
    def loop():
        time.sleep(240)  # 等預載
        while True:
            try:
                tick()
            except Exception as e:  # noqa: BLE001
                STATE["error"] = str(e)[:160]
                log.exception("總報告背景工作失敗")
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True).start()
