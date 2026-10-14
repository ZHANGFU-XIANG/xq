"""交易日曆與注意事項：未來 N 個交易日內，跟持股、期貨與競賽規則有關的事件（官方公開資料，程式整理，不經 LLM）。

- 期貨／選擇權結算日（第三個星期三，futures_data.settlement_date）：結算日前後波動大，持有部位要決定轉倉或平倉
- 持股除權息（證交所 TWT48U、櫃買 tpex_exright_prepost）：除息當天股價扣掉股利，看起來下跌但不是虧損
- 處置股（證交所 punish、櫃買 tpex_disposal_information）：分盤撮合、可能預收款，期間內流動性差
- 競賽關鍵日（config/rules.yaml）：建倉期結束、每日交易標的數檢查開始、最後一天收盤規則
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time

import requests

from src.config import is_trading_day, now_tw, rules, stock_meta

log = logging.getLogger(__name__)
_CACHE: dict = {}


def _get(url: str, ttl: float = 6 * 3600) -> list[dict]:
    hit = _CACHE.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        v = requests.get(url, timeout=30).json()
    except Exception as e:  # noqa: BLE001
        log.warning("日曆資料失敗 %s：%s", url, e)
        v = hit[1] if hit else []
    _CACHE[url] = (time.time(), v)
    return v


def roc(s: str) -> dt.date | None:
    """'1151015' 或 '115/10/15' → 2026-10-15。"""
    m = re.match(r"^(\d{2,3})/?(\d{2})/?(\d{2})$", (s or "").strip())
    if not m:
        return None
    return dt.date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))


def _window(today: dt.date, n: int) -> dt.date:
    d, k = today, 0
    while k < n:
        d += dt.timedelta(days=1)
        k += is_trading_day(d)
    return d


def events(codes: list[str], derivs: list[str], today: dt.date | None = None, n_days: int = 5) -> list[dict]:
    today = today or now_tw().date()
    end = _window(today, n_days)
    out: list[dict] = []
    held = set(codes)

    def add(d: dt.date | None, kind: str, title: str, note: str, level: str = "注意", code: str | None = None):
        if d and today <= d <= end:
            out.append({"date": d.isoformat(), "kind": kind, "title": title, "note": note, "level": level, "code": code})

    # 期貨結算
    try:
        from src.agents import futures_data

        for ym in {futures_data.front_month(today)} | {c[-6:] for c in derivs if c[-6:].isdigit()}:
            sd = futures_data.settlement_date(ym)
            has = any(c.endswith(ym) for c in derivs)
            add(sd, "期貨結算", f"{ym[:4]}/{ym[4:]} 台指期／選擇權結算日",
                ("你持有這個月份的期貨：結算價＝結算日 13:00～13:30 加權指數的平均，當天 13:30 停止交易；要續抱請在結算日前轉倉到下個月"
                 if has else "結算前後大盤波動通常較大"), "重要" if has else "注意")
    except Exception as e:  # noqa: BLE001
        log.info("結算日失敗：%s", e)
    # 除權息
    for x in _get("https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL"):
        if x.get("Code") in held:
            cash = x.get("CashDividend") or ""
            add(roc(x.get("Date", "")), "除權息", f"{x['Code']} {x.get('Name', '')} 除{x.get('Exdividend', '')}",
                f"現金股利 {float(cash):g} 元／股：除息日股價會扣掉股利，看起來下跌但不是虧損" if cash else "除權息：股價會調整",
                "注意", x["Code"])
    for x in _get("https://www.tpex.org.tw/openapi/v1/tpex_exright_prepost"):
        if x.get("SecuritiesCompanyCode") in held:
            add(roc(x.get("ExRrightsExDividendDate", "")), "除權息",
                f"{x['SecuritiesCompanyCode']} {x.get('CompanyName', '')} {x.get('ExRrightsExDividend', '')}",
                f"現金股利 {float(x.get('CashDividend') or 0):g} 元／股", "注意", x["SecuritiesCompanyCode"])
    # 處置股（持股）：期間內每天都提醒，結束日另外提示
    disp = [(x.get("Code"), x.get("Name"), x.get("DispositionPeriod", "")) for x in
            _get("https://openapi.twse.com.tw/v1/announcement/punish")]
    disp += [(x.get("SecuritiesCompanyCode"), x.get("CompanyName"), x.get("DispositionPeriod", "")) for x in
             _get("https://www.tpex.org.tw/openapi/v1/tpex_disposal_information")]
    for code, name, period in disp:
        if code not in held:
            continue
        parts = re.split(r"[～~]", period)
        if len(parts) == 2:
            a, b = roc(parts[0].replace("/", "")) or roc(parts[0]), roc(parts[1].replace("/", "")) or roc(parts[1])
            if a and b and a <= end and b >= today:
                add(max(a, today), "處置股", f"{code} {name} 處置中（{a:%m/%d}～{b:%m/%d}）",
                    "分盤撮合（約每 5～20 分鐘撮合一次）、可能要預收款；掛單要提早、價格要放寬", "重要", code)
    # 競賽關鍵日
    c = rules().get("competition", {})
    for key, title, note in (
            ("build_period_end", "建倉期最後一天", "之後每天收盤持股率 < 70% 會扣 0.2% 報酬率"),
            ("symbols_check_start", "開始每日檢查「交易過的標的數 ≥ 5」", "未達每天扣 0.2%"),
            ("end", "競賽最後一天", "收盤後必須仍持有 ≥ 5 檔不同股票、現金 < 30%，否則不列入排名")):
        v = c.get(key)
        if v:
            add(dt.date.fromisoformat(str(v)), "競賽", title, note, "重要")
    out.sort(key=lambda e: (e["date"], {"重要": 0, "注意": 1}.get(e["level"], 2)))
    for e in out:
        if e.get("code"):
            e["name"] = stock_meta(e["code"]).get("name")
    return out
