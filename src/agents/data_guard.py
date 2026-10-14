"""DataGuardAgent：每次產生建議前檢查資料是否最新、是否和官方一致，能補就補、補不了就明確警告。

為什麼要有（2026-10-14）：GitHub Actions 盤後沿用 13:05 盤中快取，10/08、10/12、10/13 三天的盤後報告用了前一天的價格，
漲跌歸因空白、淨值紀錄錯誤、隔日預掛單也用舊資料算，過了好幾天才被發現。Yahoo 限流（429）時也會靜默缺資料。

檢查（數字全由程式比對，不經 LLM）：
1. 持股與選股池的日 K 最後一天 = config.last_closed_session()；缺的用證交所 MIS 當日開高低收補上（收盤後到隔日開盤前 MIS 都是最近一盤）
2. 最後一根收盤與 MIS 相差 > 0.5% → 以 MIS（交易所即時）為準並記錄
3. 加權指數最後一天也要是最近交易日（market_data.taiex 已會用 MIS 補）
4. 期貨持倉需要有最近交易日的結算價
結果等級：ok／corrected（已自動校正）／severe（持股仍缺價格：報告標示「資料不完整，勿依此下單」，盤後不寫淨值紀錄）
"""
from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from src.config import last_closed_session, now_tw

log = logging.getLogger(__name__)
TOL = 0.005
STATE: dict = {"last": None}


def _mis_bar(q: dict, day: pd.Timestamp) -> dict | None:
    d = q.get("date") or ""
    if len(d) != 8 or pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]}") != day:
        return None
    if not (q.get("price") and q.get("open") and q.get("high") and q.get("low")):
        return None
    return {"Open": float(q["open"]), "High": float(max(q["high"], q["price"])), "Low": float(min(q["low"], q["price"])),
            "Close": float(q["price"]), "Volume": float((q.get("volume_lots") or 0) * 1000)}


def check(panel: dict[str, pd.DataFrame], codes: list[str], held: list[str], taiex: pd.DataFrame | None = None,
          derivs: list[str] | None = None, now: dt.datetime | None = None, fix: bool = True) -> dict:
    """檢查並（fix=True 時）直接修補 panel。codes：要檢查的選股池；held：持股（缺價格算嚴重）。"""
    from src.agents import alerts

    now = now or now_tw()
    latest = pd.Timestamp(last_closed_session(now))
    trading_now = alerts.is_trading_hours()
    want = list(dict.fromkeys([c for c in held + codes if c in panel]))
    stale = [c for c in want if panel[c].index[-1] < latest]
    issues, corrections, still = [], {}, []
    # 只有持股和最多 400 檔落後的股票向 MIS 查（每批 80 檔）；大量落後代表整批資料過期
    probe = list(dict.fromkeys([c for c in held if c in panel] + stale[:400]))
    rt = alerts.realtime(probe) if probe else {}
    if len(stale) > len(want) * 0.5 and want:
        issues.append({"level": "警告", "msg": f"{len(stale)}/{len(want)} 檔日 K 缺 {latest.date()}（資料源尚未更新），已用證交所即時資料補上"})
    for c in stale:
        bar = _mis_bar(rt.get(c) or {}, latest)
        if bar and fix and not trading_now:  # 盤中不補（今天還沒收盤，最後一根由各模組自己用即時價處理）
            df = panel[c]
            panel[c] = pd.concat([df, pd.DataFrame([bar], index=[latest])]).sort_index()
            corrections[c] = {"type": "補最新 K 棒", "close": bar["Close"]}
        elif not trading_now:
            still.append(c)
    if not trading_now:  # 收盤後：最後一根和 MIS 不一致就以交易所為準
        for c in [c for c in held if c in panel and c not in corrections]:
            q = rt.get(c) or {}
            bar = _mis_bar(q, latest)
            if not bar or panel[c].index[-1] != latest:
                continue
            yc = float(panel[c].Close.iloc[-1])
            if yc > 0 and abs(bar["Close"] / yc - 1) > TOL:
                if fix:
                    for k, v in bar.items():
                        if k != "Volume":
                            panel[c].loc[latest, k] = v
                corrections[c] = {"type": "收盤價校正", "from": yc, "close": bar["Close"]}
    held_missing = [c for c in held if c in still or c not in panel]
    if taiex is not None and len(taiex) and taiex.index[-1] < latest and not trading_now:
        issues.append({"level": "嚴重", "msg": f"加權指數缺 {latest.date()} 收盤"})
    if derivs and not trading_now:
        try:
            from src.agents import futures_data

            h = futures_data.history("TX", 5)
            if not h or h[-1]["time"] != latest.date().isoformat() or not h[-1].get("settlement"):
                issues.append({"level": "警告", "msg": f"期貨缺 {latest.date()} 結算價，期貨部位以最近報價估算"})
        except Exception as e:  # noqa: BLE001
            issues.append({"level": "警告", "msg": f"期貨結算價查詢失敗：{str(e)[:80]}"})
    if held_missing:
        issues.append({"level": "嚴重", "msg": f"持股缺 {latest.date()} 價格：{'、'.join(held_missing)}（資料不完整，勿依此下單）"})
    if still and len(still) > len(held_missing):
        issues.append({"level": "警告", "msg": f"另有 {len(still) - len(held_missing)} 檔選股池股票缺最新 K 棒，已排除在今日排名外不影響持股"})
    if corrections:
        n_fix = sum(1 for v in corrections.values() if v["type"] == "收盤價校正")
        issues.append({"level": "提醒", "msg": f"已自動校正 {len(corrections)} 檔（補 K 棒 {len(corrections) - n_fix}、收盤價校正 {n_fix}），以證交所即時資料為準"})
    severe = any(i["level"] == "嚴重" for i in issues)
    out = {"time": now.strftime("%Y-%m-%d %H:%M"), "latest": str(latest.date()), "trading": trading_now,
           "level": "severe" if severe else "corrected" if corrections else "ok", "issues": issues,
           "corrections": corrections, "checked": len(want), "stale_after": still, "held_missing": held_missing}
    STATE["last"] = out
    if severe:
        log.error("DataGuard：%s", "；".join(i["msg"] for i in issues if i["level"] == "嚴重"))
    return out


def banner(res: dict | None) -> str | None:
    """報告／網站最上方的一句話（ok 時不顯示）。"""
    if not res or res["level"] == "ok":
        return None
    if res["level"] == "severe":
        return "⚠ 資料不完整，勿依此下單：" + "；".join(i["msg"] for i in res["issues"] if i["level"] == "嚴重")
    return "資料已校正：" + "；".join(i["msg"] for i in res["issues"] if i["level"] in ("提醒", "警告"))
