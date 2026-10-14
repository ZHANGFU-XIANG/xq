"""FinReportAgent：每天檢查上市櫃公司是否公布新財報，算出財報變化並修正篩選與持股提醒。

資料（官方 OpenAPI，全部公司最新一季，每天更新）：
- 證交所 t187ap14_L／櫃買 mopsfin_t187ap14_O：EPS、營收、營業利益、稅後淨利（含出表日期）
- 證交所 t187ap06_L_ci／櫃買 mopsfin_t187ap06_O_ci：營業毛利（算毛利率）
- 去年同季：公開資訊觀測站財報（mops.quarterly，FinMind），只查持股、前 40 名與當天新公布的公司
新財報判斷：data/fin_reports.csv 記錄看過的（公司, 年度, 季別），沒看過的就是新公布。

對評分與篩選的影響（使用者 2026-10-15 要求「修正評分與篩選建議」）：
- 最新財報「轉虧」（EPS < 0 且去年同季 ≥ 0）→ 綜合排名結論改「先不要買」（風險過濾，和處置股排除同性質）
- EPS 年增 ≥ 30% 且營收年增 > 0 → 標「財報利多」；EPS 年減 ≥ 30% → 標「財報利空」，持股會收到提醒
- 這兩個標記暫時不加減分數：財報後漂移（PEAD）的加權要等模型研究 Agent 用歷史資料驗證後才採用
"""
from __future__ import annotations

import csv
import logging
import time

import requests

from src.config import DATA_DIR, now_tw, stock_meta

log = logging.getLogger(__name__)
STORE = DATA_DIR / "fin_reports.csv"
FIELDS = ["code", "year", "quarter", "eps", "revenue", "op_income", "net_income", "gross_profit", "published", "seen"]
URLS = {
    "L_eps": "https://openapi.twse.com.tw/v1/opendata/t187ap14_L",
    "O_eps": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap14_O",
    "L_ci": "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci",
    "O_ci": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ci",
}
_CACHE: dict = {}


def _f(x) -> float | None:
    try:
        return float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _get(url: str) -> list[dict]:
    hit = _CACHE.get(url)
    if hit and time.time() - hit[0] < 3 * 3600:
        return hit[1]
    try:
        v = requests.get(url, timeout=40).json()
    except Exception as e:  # noqa: BLE001
        log.warning("財報資料失敗 %s：%s", url, e)
        v = hit[1] if hit else []
    _CACHE[url] = (time.time(), v)
    return v


def latest() -> dict[str, dict]:
    """{code: 最新一季}（上市＋上櫃）。"""
    out: dict[str, dict] = {}
    for key in ("L_eps", "O_eps"):
        for x in _get(URLS[key]):
            code = x.get("公司代號") or x.get("SecuritiesCompanyCode")
            if not code:
                continue
            out[code] = {"code": code, "year": int(_f(x.get("年度") or x.get("Year")) or 0) + 1911,
                         "quarter": int(_f(x.get("季別")) or 0), "eps": _f(x.get("基本每股盈餘(元)") or x.get("基本每股盈餘")),
                         "revenue": _f(x.get("營業收入")), "op_income": _f(x.get("營業利益")),
                         "net_income": _f(x.get("稅後淨利")), "published": x.get("出表日期") or x.get("Date")}
    for key in ("L_ci", "O_ci"):
        for x in _get(URLS[key]):
            code = x.get("公司代號") or x.get("SecuritiesCompanyCode")
            if code in out:
                out[code]["gross_profit"] = _f(x.get("營業毛利（毛損）淨額") or x.get("營業毛利（毛損）"))
    return out


def _seen() -> dict[tuple, dict]:
    if not STORE.exists():
        return {}
    return {(r["code"], int(r["year"]), int(r["quarter"])): r for r in csv.DictReader(open(STORE, encoding="utf-8"))}


def _write(rows: list[dict]) -> None:
    with open(STORE, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def _yoy(code: str, cur: dict) -> dict:
    """去年同期（年初至今累計）：OpenAPI 的 EPS 與營收是累計數（第 2 季＝上半年），所以要和去年 Q1～Qn 的加總比。
    先看本機紀錄（同為累計數），沒有就用公開資訊觀測站單季財報（FinMind）加總。"""
    seen = _seen().get((code, cur["year"] - 1, cur["quarter"]))
    prev_eps = _f(seen["eps"]) if seen else None
    prev_rev = _f(seen["revenue"]) if seen else None
    if prev_eps is None:
        try:
            from src.agents import mops

            q = {x["quarter"]: x for x in mops.quarterly(code, 8)}
            parts = [q.get(f"{cur['year'] - 1}Q{i}") for i in range(1, cur["quarter"] + 1)]
            if all(parts) and all(p.get("eps") is not None for p in parts):
                prev_eps = round(sum(p["eps"] for p in parts), 2)
                prev_rev = sum((p.get("revenue_100m") or 0) for p in parts) * 1e8 / 1000  # OpenAPI 營收單位為千元
        except Exception as e:  # noqa: BLE001
            log.info("去年同期查詢失敗 %s：%s", code, e)
    return {"prev_eps": prev_eps, "prev_rev": prev_rev}


def assess(cur: dict) -> dict:
    y = _yoy(cur["code"], cur)
    eps, pe = cur.get("eps"), y["prev_eps"]
    rev, pr = cur.get("revenue"), y["prev_rev"]
    eps_yoy = (eps - pe) / abs(pe) * 100 if eps is not None and pe not in (None, 0) else None
    rev_yoy = (rev / pr - 1) * 100 if rev and pr else None
    gm = cur["gross_profit"] / rev * 100 if cur.get("gross_profit") is not None and rev else None
    flags = []
    if eps is not None and eps < 0 and (pe is None or pe >= 0):
        flags.append("轉虧")
    if eps is not None and pe is not None and pe < 0 <= eps:
        flags.append("轉盈")
    if eps_yoy is not None and eps_yoy >= 30 and pe is not None and pe > 0 and (rev_yoy is None or rev_yoy > 0):
        flags.append("財報利多")
    if eps_yoy is not None and eps_yoy <= -30:
        flags.append("財報利空")
    q = f"{cur['year']}Q{cur['quarter']}"
    ytd = "上半年" if cur["quarter"] == 2 else "前三季" if cur["quarter"] == 3 else "全年" if cur["quarter"] == 4 else "第一季"
    text = (f"{q}（{ytd}累計）EPS {eps}" + (f"（去年同期 {pe}，{eps_yoy:+.0f}%）" if eps_yoy is not None else "")
            + (f"、營收年增 {rev_yoy:+.1f}%" if rev_yoy is not None else "") + (f"、毛利率 {gm:.1f}%" if gm is not None else ""))
    return {**cur, "quarter_label": q, "prev_eps": pe, "eps_yoy_pct": round(eps_yoy, 1) if eps_yoy is not None else None,
            "rev_yoy_pct": round(rev_yoy, 1) if rev_yoy is not None else None, "gross_margin": round(gm, 2) if gm is not None else None,
            "flags": flags, "name": stock_meta(cur["code"]).get("name"), "text": text}


def run(focus: list[str] | None = None, write: bool = False) -> dict:
    """focus：持股＋推薦等要詳細判讀的代號。write：把新看到的財報寫進 data/fin_reports.csv（GitHub Actions 盤前／盤後）。"""
    from src.state import portfolio as pfm

    key = now_tw().strftime("%Y%m%d%H")
    if key in _CACHE and not write:
        return _CACHE[key]
    lat = latest()
    seen = _seen()
    first = not seen  # 第一次執行：只建立基準，不把所有公司都當成「今天新公布」
    new = [] if first else [v for k, v in lat.items() if (k, v["year"], v["quarter"]) not in seen and v["year"] > 1911]
    held = list(pfm.build_portfolio().positions)
    focus = list(dict.fromkeys(held + (focus or [])))
    detail_codes = list(dict.fromkeys([c for c in focus if c in lat] + [v["code"] for v in new][:40]))
    detail = {c: assess(lat[c]) for c in detail_codes}
    if write and (new or first):
        today = now_tw().date().isoformat()
        add = list(lat.values()) if first else new
        rows = list(seen.values()) + [{**{k: v.get(k) for k in FIELDS}, "seen": today} for v in add if v["year"] > 1911]
        _write(rows)
    alerts_ = []
    for c in held:
        d = detail.get(c)
        if d and d["flags"] and any(v["code"] == c for v in new):
            bad = any(f in ("轉虧", "財報利空") for f in d["flags"])
            alerts_.append({"key": f"fin-{c}-{d['quarter_label']}", "level": "嚴重" if "轉虧" in d["flags"] else "注意",
                            "title": f"持股 {c} {d['name']} 公布財報：{'、'.join(d['flags'])}",
                            "body": d["text"] + ("；財報轉弱，留意是否跌破停損" if bad else "")})
    out = {"time": now_tw().strftime("%m/%d %H:%M"), "n_companies": len(lat), "new_count": len(new),
           "new": [detail.get(v["code"]) or {**v, "name": stock_meta(v["code"]).get("name")} for v in new[:40]],
           "focus": [detail[c] for c in focus if c in detail], "alerts": alerts_,
           "note": "最新財報轉虧的股票在綜合排名改為「先不要買」；財報利多／利空目前只標示，不加減分數（等歷史驗證）"}
    _CACHE[key] = out
    return out


def flags_for(codes: list[str]) -> dict[str, dict]:
    """綜合排名用：只看最新一季是否轉虧（便宜、可快取），不逐檔查去年同季以外的資料。"""
    lat = latest()
    out = {}
    for c in codes:
        cur = lat.get(c)
        if not cur or cur.get("eps") is None or cur["eps"] >= 0:
            continue
        key = ("flag", c, cur["year"], cur["quarter"])
        if key not in _CACHE:
            _CACHE[key] = (time.time(), assess(cur))
        d = _CACHE[key][1]
        if "轉虧" in d["flags"]:
            out[c] = d
    return out
