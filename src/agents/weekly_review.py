"""WeeklyReviewAgent：週報產生後、輸出 PPT 前的檢查關卡。

檢查三類問題，能自動修正的直接修正，修不了的列在週報最後的「週報檢查結果」頁：
- 數據：週報酬、每日總資產、損益來源加總、加權指數、成交金額、持股市值、持股率彼此是否一致
- 新聞：每則事件都要有新聞連結；新聞時間或收錄報告晚於當天 → 移除（不可用未來新聞解釋當天盤勢）；
  同一則事件跨日重複 → 只留第一天
- 理由：理由出處是否在成交前就已產生（盤後報告不能當當天成交的理由）、理由與買賣方向是否矛盾、
  「停利」卻是虧損賣出等前後衝突、成交價與建議價差距；AI 文字的交易說明與買賣方向、每日盤勢與漲跌是否矛盾
全部用規則與系統紀錄比對，不靠 LLM 判斷。
"""
from __future__ import annotations

import datetime as dt
import logging
import re

log = logging.getLogger(__name__)

OK, FIXED, WARN, ERR = "通過", "已修正", "注意", "錯誤"
BUY_WORDS = ("買進候選", "布局", "加碼", "承接", "建倉", "跟進")
SELL_WORDS = ("停利", "停損", "減碼", "出場", "賣出", "了結", "調節")
UP_WORDS = ("上漲", "收紅", "收高", "走揚", "反彈", "大漲")
DOWN_WORDS = ("下跌", "收黑", "收低", "走跌", "重挫", "大跌", "回檔")


class _Log:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, group: str, item: str, status: str, detail: str = "") -> None:
        self.rows.append({"group": group, "item": item, "status": status, "detail": detail})


# ---------------------------------------------------------------- 數據
def _check_numbers(w: dict, lg: _Log) -> None:
    g = "數據"
    if w["nav_start"]:
        calc = round((w["nav_end"] / w["nav_start"] - 1) * 100, 2)
        ok = w["week_ret_pct"] is not None and abs(calc - w["week_ret_pct"]) <= 0.01
        if not ok:
            w["week_ret_pct"] = calc
        lg.add(g, "週報酬＝期末÷期初−1", OK if ok else FIXED,
               f"{w['nav_start']:,} → {w['nav_end']:,}，{calc:+.2f}%")
    navs = [x for x in w["daily"] if x.get("nav") is not None]
    if navs:
        ok = navs[-1]["nav"] == w["nav_end"]
        lg.add(g, "最後交易日總資產＝期末總資產", OK if ok else ERR,
               f"{navs[-1]['date']}：{navs[-1]['nav']:,}" + ("" if ok else f"，期末 {w['nav_end']:,}"))
    if "contrib_total" in w:
        diff = w["contrib_total"] - (w["nav_end"] - w["nav_start"])
        lg.add(g, "各檔損益加總＝總資產變動", OK if abs(diff) <= 5 else WARN,
               f"加總 {w['contrib_total']:+,}，總資產變動 {w['nav_end'] - w['nav_start']:+,}" + (f"，差 {diff:+,}" if abs(diff) > 5 else ""))
    chg = [x["taiex_chg_pct"] for x in w["daily"] if x.get("taiex_chg_pct") is not None]
    if chg and w.get("taiex_week_pct") is not None:
        prod = 1.0
        for c in chg:
            prod *= 1 + c / 100
        gap = (prod - 1) * 100 - w["taiex_week_pct"]
        lg.add(g, "加權指數每日漲跌連乘＝本週漲跌", OK if abs(gap) <= 0.05 else ERR,
               f"每日連乘 {(prod - 1) * 100:+.2f}%，本週 {w['taiex_week_pct']:+.2f}%")
    bad = []
    from src.state import portfolio as pfm

    for t in w["trades"]:
        if t["asset"] in ("FUT", "OPT"):
            exp = round(t["qty"] * t["price"] * pfm.deriv_spec(t["code"].upper())["multiplier"])
        else:
            exp = t["qty"] * t["price"]
        if exp and abs(t["amount"] - exp) / exp > 0.005:
            bad.append(f"{t['date'][5:]} {t['code']}：{t['amount']:,} 應約 {exp:,.0f}")
    lg.add(g, "成交金額＝數量×價格", ERR if bad else OK, "；".join(bad[:3]) or f"{len(w['trades'])} 筆皆一致")
    bad = [h["code"] for h in w["holdings"] if h["asset"] in ("STOCK", "ETF") and h["price"]
           and abs(h["value"] - h["shares"] * h["price"]) > 1]
    lg.add(g, "持股市值＝股數×收盤價", ERR if bad else OK, "、".join(bad) or f"{len(w['holdings'])} 檔皆一致")
    r = w["holding_ratio"]
    if r > 1:
        lg.add(g, "持股率", WARN, f"{r * 100:.1f}% 超過 100%：期貨未實現虧損讓分母變小（不是融資），文字已加註")
    else:
        lg.add(g, "持股率", OK if r >= 0.7 else WARN, f"{r * 100:.1f}%（規則下限 70%）")


# ---------------------------------------------------------------- 新聞
def _news_date(n: dict, day: dt.date) -> dt.date | None:
    m = re.match(r"(\d{1,2})/(\d{1,2})", n.get("time") or "")
    if m:
        try:
            return dt.date(day.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    rep = (n.get("report") or "")[:10]
    return dt.date.fromisoformat(rep) if rep else None


def _check_news(w: dict, lg: _Log) -> list[dict]:
    """移除沒有出處、比當天還晚、跨日重複的事件；回傳本週引用的全部新聞（給 PPT 附連結頁）。"""
    g = "新聞"
    no_src, future, dup, kept = [], [], [], 0
    seen: set[str] = set()
    used: list[dict] = []
    markets = [x for x in w["daily"]] + [t["market"] for t in w["trades"]]
    clean: dict[tuple, list] = {}
    for x in w["daily"]:
        day = dt.date.fromisoformat(x["date"])
        for key in ("events", "macro"):
            keep = []
            for e in x.get(key) or []:
                news = [n for n in e.get("news") or [] if n.get("url")]
                if not news:
                    no_src.append(e["event"])
                    continue
                news = [n for n in news if (nd := _news_date(n, day)) is None or nd <= day]
                if not news:
                    future.append(f"{x['date'][5:]} {e['event'][:20]}")
                    continue
                if e["event"] in seen:
                    dup.append(e["event"][:20])
                    continue
                seen.add(e["event"])
                keep.append({**e, "news": news})
                if x["trading"] and x.get("nav") is not None:  # 還沒開盤的日子不列入新聞來源頁
                    used.append({"date": x["date"], "weekday": x["weekday"], "event": e["event"], "news": news})
            clean[(x["date"], key)] = keep
            kept += len(keep)
    for m in markets:  # 每日資料與交易頁的「當天市場」共用同一套清理結果
        for key in ("events", "macro"):
            if (m["date"], key) in clean:
                m[key] = clean[(m["date"], key)]
    lg.add(g, "每則事件都有新聞連結", FIXED if no_src else OK,
           f"移除 {len(no_src)} 則沒有出處的事件" if no_src else f"{kept} 則事件皆附連結")
    lg.add(g, "新聞時間不晚於當天", FIXED if future else OK,
           ("移除：" + "；".join(future[:3])) if future else "沒有引用未來新聞")
    lg.add(g, "事件不跨日重複", FIXED if dup else OK, f"重複的 {len(set(dup))} 則只保留第一天" if dup else "無重複")
    return used


# ---------------------------------------------------------------- 理由
def _avg_cost_before(code: str, day: str) -> float | None:
    from src.state import portfolio as pfm

    try:
        pf = pfm.build_portfolio([t for t in pfm.load_trades() if t["date"] < day])
    except Exception:  # noqa: BLE001
        return None
    p = pf.positions.get(code)
    return p.avg_cost if p else None


def _check_reasons(w: dict, lg: _Log) -> None:
    from src.agents import weekly

    g = "理由"
    late, conflict, pnl_bad, gaps = [], [], [], []
    for t in w["trades"]:
        r = t["reason"]
        tag = f"{t['date'][5:]} {'買' if t['side'] == 'BUY' else '賣'} {t['code']}"
        if (r.get("generated_at") or "") >= f"{t['date']} {weekly.CLOSE_TIME}":
            late.append(tag)
            t["reason"] = r = weekly._infer_reason(t, [], t["market"], {})
        wrong = SELL_WORDS if t["side"] == "BUY" else BUY_WORDS
        keep = []
        for x in r.get("reasons", []):
            # 賣出理由常寫「減碼…保留部位」、買進理由常寫「停損設在…」，只抓句首方向明確相反的
            if any(x.startswith(k) or f"，{k}" in x[:12] for k in wrong):
                conflict.append(f"{tag}：{x[:18]}")
                continue
            keep.append(x)
        if len(keep) != len(r.get("reasons", [])):
            r["reasons"] = keep or weekly._infer_reason(t, [], t["market"], {})["reasons"]
        if t["side"] == "SELL" and t["asset"] in ("STOCK", "ETF"):
            cost = _avg_cost_before(t["code"], t["date"])
            if cost:
                pct = (t["price"] / cost - 1) * 100
                fixed = []
                for x in r["reasons"]:
                    if ("停利" in x and pct < 0) or ("停損" in x and pct > 0):
                        pnl_bad.append(f"{tag}：寫{'停利' if '停利' in x else '停損'}，實際 {pct:+.1f}%")
                        x += f"（成交價 {t['price']:,} 相對成本 {cost:,.2f} 為 {pct:+.1f}%）"
                    fixed.append(x)
                r["reasons"] = fixed
        if r.get("plan_price"):
            gap = (t["price"] / r["plan_price"] - 1) * 100
            if abs(gap) > 5:
                gaps.append(f"{tag} 成交 {t['price']:,} 建議 {r['plan_price']:,}（{gap:+.1f}%）")
    lg.add(g, "理由出處在成交前產生", FIXED if late else OK,
           ("改用成交前事實重寫：" + "、".join(late)) if late else "皆引用收盤前已產生的報告或成交前資料")
    lg.add(g, "理由與買賣方向一致", FIXED if conflict else OK, ("移除：" + "；".join(conflict[:3])) if conflict else "無矛盾")
    lg.add(g, "停利／停損與實際損益一致", FIXED if pnl_bad else OK, "；".join(pnl_bad[:3]) + ("，已加註實際損益" if pnl_bad else "無衝突"))
    lg.add(g, "成交價與建議價差距 ≤ 5%", WARN if gaps else OK, "；".join(gaps[:3]) or "皆在 5% 內")
    both = sorted({t["code"] for t in w["trades"] if t["side"] == "BUY"} & {t["code"] for t in w["trades"] if t["side"] == "SELL"})
    if both:
        lg.add(g, "同週買賣同一檔", WARN, "、".join(both) + "：買進與賣出理由需能互相解釋")


# ---------------------------------------------------------------- AI 文字
def _check_text(w: dict, text, lg: _Log):
    from src.agents import weekly

    g = "文字"
    trades = {(t["code"], t["date"]): t for t in w["trades"]}
    notes, fixed = [], []
    for n in text.trade_notes:
        t = trades.get((n.code, n.date))
        if t is None:
            fixed.append(f"{n.date[5:]} {n.code} 不在成交紀錄")
            continue
        wrong = SELL_WORDS if t["side"] == "BUY" else BUY_WORDS
        if any(k in n.comment for k in wrong) and not any(k in n.comment for k in (SELL_WORDS if t["side"] == "SELL" else BUY_WORDS)):
            fixed.append(f"{n.date[5:]} {n.code} 說明與{'買進' if t['side'] == 'BUY' else '賣出'}方向矛盾")
            n = weekly.TradeNote(code=n.code, date=n.date, comment="；".join(t["reason"]["reasons"][:2]) or t["reason"]["source"])
        notes.append(n)
    text.trade_notes = notes
    lg.add(g, "交易說明與成交紀錄一致", FIXED if fixed else OK, "；".join(fixed[:3]) or f"{len(notes)} 則皆一致")
    days = {f"週{x['weekday']}": x for x in w["daily"] if x["trading"]}
    bad = []
    lines = []
    for line in text.market_review:
        x = days.get(line[:2])
        chg = x and x.get("taiex_chg_pct")
        if chg is not None and ((chg < 0 and any(k in line for k in UP_WORDS) and not any(k in line for k in DOWN_WORDS))
                                or (chg > 0 and any(k in line for k in DOWN_WORDS) and not any(k in line for k in UP_WORDS))):
            bad.append(line[:2])
            line = f"{line[:2]}：加權 {chg:+.2f}%" + (f"；{x['events'][0]['event']}" if x.get("events") else "")
        lines.append(line)
    text.market_review = lines
    lg.add(g, "每日盤勢描述與實際漲跌一致", FIXED if bad else OK, ("改寫：" + "、".join(bad)) if bad else "一致")
    margin = any("融資" in (t.get("note") or "") for t in w["trades"])

    def wrong_margin(x: str) -> bool:  # 持股率超過 100% 被誤解成融資／槓桿
        return not margin and ("融資" in x or ("持股率" in x and "槓桿" in x))

    cut = []
    for field in ("summary", "lessons"):
        cut += [x for x in getattr(text, field) if wrong_margin(x)]
        setattr(text, field, [x for x in getattr(text, field) if not wrong_margin(x)])
    if wrong_margin(text.headline):
        cut.append(text.headline)
        text.headline = weekly._template(w, {}).headline
    lg.add(g, "沒有錯誤描述融資", FIXED if cut else OK, f"刪除 {len(cut)} 句" if cut else "無")
    return text


def check_data(w: dict) -> dict:
    """產生文字前：檢查並就地修正數據、理由、新聞，讓 AI 只拿到乾淨的事實。"""
    lg = _Log()
    for step in (_check_numbers, _check_reasons, _check_news):
        try:
            out = step(w, lg)
        except Exception as e:  # noqa: BLE001
            log.warning("週報檢查 %s 失敗：%s", step.__name__, e)
            lg.add("系統", step.__name__, ERR, str(e)[:60])
            out = None
        if step is _check_news:
            news = out or []
    return {"checks": lg.rows, "news": news}


def check_text(w: dict, text, verified: bool, rep: dict):
    """產生文字後：檢查 AI 文字與事實是否矛盾並修正，補上總結。回傳修正後文字。"""
    lg = _Log()
    text = _check_text(w, text, lg)
    lg.add("文字", "AI 文字數字核對", OK if verified else WARN, "所有數字可在資料中找到" if verified else "未通過核對，改用固定格式文字")
    rep["checks"] += lg.rows
    rep["counts"] = {s: sum(1 for r in rep["checks"] if r["status"] == s) for s in (OK, FIXED, WARN, ERR)}
    rep["passed"] = rep["counts"][ERR] == 0
    return text
