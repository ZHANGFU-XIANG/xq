"""ETFAgent：ETF 籃的分類、評分與「不追溢價」檢查。

- 範圍：流動性股票池內的上市櫃 ETF；槓桿、反向、債券、期貨型已在 universe 排除（settings.universe.exclude_name_keywords）。
- 分類：市值型、高股息、產業主題、其他（依名稱關鍵字），同類最多選 max_per_category 檔以分散。
- 評分：沿用一個月模型的收正機率、預期回檔與波動（同穩健籃元件），再加上近 60 日趨勢。
- 溢價：證交所 ETF 即時預估淨值（mis.twse.com.tw/stock/data/all_etf.txt），溢價超過上限不追。
"""
from __future__ import annotations

import logging
import time

import requests

from src.config import stock_meta
from src.data import providers
from src.state import params

log = logging.getLogger(__name__)
CATEGORIES = [
    ("高股息", ("高股息", "高息", "股息", "收益", "優息", "高利", "息收")),
    ("市值型", ("台灣50", "臺灣50", "中型100", "台灣領袖", "市值", "MSCI台灣", "臺灣卓越", "台灣卓越", "富時台灣", "台灣高股息")),
    ("產業主題", ("半導體", "科技", "5G", "電動車", "AI", "網通", "金融", "生技", "ESG", "電子", "智能", "未來車", "IC", "創新",
                  "數位", "雲端", "機器人", "低碳", "綠能", "航運")),
]
MARKET_CAP_CODES = {"0050", "006208", "0051", "006204", "00692", "00850"}
_PREM: dict = {"t": 0.0, "v": {}}


def category(code: str, name: str) -> str:
    if code in MARKET_CAP_CODES:
        return "市值型"
    for cat, kws in CATEGORIES:
        if any(k in name for k in kws):
            return cat
    return "其他"


def premiums() -> dict[str, dict]:
    """{code: {price, nav, premium_pct, time}}，證交所 ETF 預估淨值（60 秒快取）。"""
    if time.time() - _PREM["t"] < 60 and _PREM["v"]:
        return _PREM["v"]
    out = {}
    try:
        j = requests.get("https://mis.twse.com.tw/stock/data/all_etf.txt", timeout=20,
                         headers={"User-Agent": "Mozilla/5.0"}).json()
        for g in j.get("a1", []):
            for x in g.get("msgArray", []):
                try:
                    out[str(x["a"]).strip()] = {"price": float(x["e"]), "nav": float(x["f"]),
                                                "premium_pct": float(x["g"]), "time": f"{x.get('i', '')} {x.get('j', '')}"}
                except (KeyError, TypeError, ValueError):
                    continue
        providers._mark("證交所 ETF 預估淨值", True)
    except Exception as e:  # noqa: BLE001
        providers._mark("證交所 ETF 預估淨值", False, str(e))
        log.warning("ETF 預估淨值失敗：%s", e)
    if out:
        _PREM.update(t=time.time(), v=out)
    return out or _PREM["v"]


def annotate(ranked: list[dict]) -> None:
    """替排行中的 ETF 加上 etf_score、etf_signal、etf_category、etf_reasons、etf_risks。"""
    cfg = params.basket("etf")
    prem = premiums()
    for s in ranked:
        if not stock_meta(s["code"]).get("is_etf"):
            s["etf_signal"] = "非 ETF"
            continue
        comps = s.get("def_components") or {}
        f = (s.get("alpha") or {}).get("features") or {}
        trend = 1.0 if (f.get("r60") or 0) > 0 else -0.5  # 近 60 日趨勢
        score = 0.40 * comps.get("up", 0) + 0.30 * comps.get("drawdown", 0) + 0.20 * comps.get("lowvol", 0) + 0.10 * trend
        cat = category(s["code"], s.get("name", ""))
        reasons = [f"【ETF 籃】類別：{cat}；ETF 證交稅 0.1%（股票 0.3%），換手成本低、分散個股風險"]
        reasons += [r for r in s.get("def_reasons", []) if not r.startswith(("基本面", "籌碼面"))]
        risks = []
        p = prem.get(s["code"])
        if p:
            reasons.append(f"折溢價：市價 {p['price']}、預估淨值 {p['nav']}，{'溢價' if p['premium_pct'] >= 0 else '折價'} "
                           f"{p['premium_pct']:+.2f}%（證交所 {p['time']}）")
        else:
            risks.append("無法取得預估淨值，折溢價無法確認")
        sig = "買進候選" if score >= cfg["buy_threshold"] else "觀望"
        if p and p["premium_pct"] > cfg["max_premium_pct"]:
            sig = "溢價不追"
            risks.append(f"溢價 {p['premium_pct']:+.2f}% > {cfg['max_premium_pct']}%：買貴了，等溢價收斂再買")
        s.update(etf_score=round(score, 3), etf_signal=sig, etf_category=cat, etf_reasons=reasons, etf_risks=risks,
                 etf_premium=p)
