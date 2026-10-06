"""手機即時通知：盤中每分鐘檢查持股動向與嚴重警示，有新狀況就寄 Email（手機 Gmail 會跳通知），網頁不用開著。

- 觸發：持股新進入「盡快賣出」或「低檔可望反彈」、新的「嚴重」警示（跌停、跌破停損、重大負面新聞等）、
  大盤黃燈／紅燈（撤資預警）、盤中加權指數跌破轉弱線（預判今天收盤變燈）
- 防洗版：同一檔同一種狀況一天只寄一次；同一分鐘的多則合併成一封
- 只在雲端模式、交易時段（09:00～13:35）執行；Space 不能連 SMTP，由 GitHub Actions send-email.yml 代寄（用 GitHub Secrets 的 SMTP 設定）
- 寄送失敗 10 分鐘後才重試，避免每分鐘重試洗版
"""
from __future__ import annotations

import logging
import threading
import time

from src.config import now_tw

log = logging.getLogger(__name__)
_sent: set[str] = set()
state: dict = {"last": None, "error": None, "count": 0}
_retry_at = 0.0


def _events() -> list[tuple[str, str, str]]:
    """(去重鍵, 標題, 內容)"""
    from src.api import data

    out = []
    w = data.holding_watch()
    for i in w.get("items", []):
        if i["status"] in ("盡快賣出", "低檔可望反彈"):
            out.append((f"{i['code']}|{i['status']}", f"{i['status']}：{i['code']} {i['name']}",
                        f"現價 {i['price']}" + (f"（{i['chg_pct']:+.2f}%）" if i.get("chg_pct") is not None else "")
                        + "\n" + "；".join(i["reasons"])))
    try:  # 大盤黃燈／紅燈與盤中「快要變燈」預警：每種狀況一天寄一次
        rg = data.live_regime()
        if rg["level"] in ("黃燈", "紅燈"):
            out.append((f"regime|{rg['level']}", rg["headline"],
                        "\n".join([*rg["warnings"], "建議：", *rg["actions"]])))
        if rg["warnings"] and rg["warnings"][0].startswith("盤中加權指數"):
            out.append((f"regime-live|{rg['warnings'][0][:30]}", "大盤可能變燈：" + rg["warnings"][0][:40], rg["warnings"][0]))
    except Exception:  # noqa: BLE001
        pass
    al = data.live_alerts()
    for a in al.get("alerts", []):
        if a.get("level") == "嚴重" and a.get("scope") in ("持股", "部位", "規則"):  # 買進前、全市場的警示不寄
            out.append((f"alert|{a.get('code', '')}|{a.get('title', '')}", f"嚴重警示：{a.get('title', '')}",
                        a.get("action", "") or "；".join(a.get("reasons", []))))
    return out


def tick() -> int:
    global _retry_at
    from src.agents import alerts
    from src.reports import emailer

    if not alerts.is_trading_hours() or time.time() < _retry_at:
        return 0
    day = now_tw().date().isoformat()
    new = [(k, t, b) for k, t, b in _events() if f"{day}|{k}" not in _sent]
    if not new:
        return 0
    subject = f"[投資競賽] {new[0][1]}" + (f" 等 {len(new)} 則" if len(new) > 1 else "")
    body = "\n\n".join(f"■ {t}\n{b}" for _, t, b in new) + f"\n\n{now_tw():%H:%M} 盤中即時通知・詳情請開網站「總覽 → 持股動向」"
    if emailer.send(subject, body, []):
        for k, _, _ in new:
            _sent.add(f"{day}|{k}")
        state.update(last=f"{now_tw():%m/%d %H:%M} {new[0][1]}", error=None, count=state["count"] + 1)
        return len(new)
    _retry_at = time.time() + 600
    state["error"] = f"{now_tw():%H:%M} Email 未寄出（GitHub 代寄失敗，10 分鐘後重試；檢查 Space 的 GITHUB_TOKEN 是否有 Actions 權限）"
    return 0


def start_background(interval: int = 60) -> None:
    def loop():
        time.sleep(120)  # 等網站預載完成
        while True:
            try:
                tick()
            except Exception as e:  # noqa: BLE001
                state["error"] = str(e)[:160]
                log.exception("即時通知失敗")
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True).start()
