"""ExecutionAgent：開盤前提醒預掛單、給「開盤就能成交」的限價，並追蹤實際成交和開盤價的差距。

依據：全歷史回測同一組參數，隔日開盤成交年化約 67%、隔日收盤才成交只剩 26%，模型的優勢大多在開盤跳空；
10/05～10/14 實際成交比當天開盤價多付約 7 萬元（影子組合拆解）。
- 08:40～08:58：列出今天報告的建議單，提醒「還沒預掛的請在 09:00 前掛好」（網站推播＋Email，一天一次）
- 建議限價：買進＝min(昨收 × 1.03, 漲停價)，賣出＝max(昨收 × 0.97, 跌停價)——讓開盤集合競價一定成交，
  成交價仍是開盤價（集合競價以單一價格撮合，限價只是上限／下限）
- 成交追蹤：shadow.actual_breakdown 的每筆 vs 開盤價
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from src.config import FACTS_DIR, is_trading_day, now_tw

log = logging.getLogger(__name__)


def todays_orders(today: dt.date) -> tuple[list[dict], str | None]:
    """今天開盤要執行的建議單：優先開盤前更新，其次盤前報告，再其次前一個盤後預掛單。"""
    for suffix in ("開盤前更新", "盤前報告"):
        fp = FACTS_DIR / f"{today.isoformat()}_{suffix}.json"
        if fp.exists():
            d = json.loads(fp.read_text(encoding="utf-8"))
            return d.get("orders", []), suffix
    for fp in sorted(FACTS_DIR.glob("*_盤後檢討與隔日預掛單.json"), reverse=True):
        d = json.loads(fp.read_text(encoding="utf-8"))
        if d.get("target_date") == today.isoformat():
            return d.get("orders", []), "盤後預掛單"
    return [], None


def open_limits(orders: list[dict], today: dt.date | None = None) -> list[dict]:
    from src.agents import alerts, sizer
    from src.state.portfolio import is_etf

    codes = [o["code"] for o in orders if o.get("asset", "STOCK") in (None, "STOCK", "ETF")]
    rt = alerts.realtime(codes) if codes else {}
    out = []
    for o in orders:
        q = rt.get(o["code"]) or {}
        today = today or now_tw().date()
        # MIS 最新一盤若是今天以前（盤前、盤後），它的成交價就是「今天開盤的昨收」；今天開盤後才用 prev_close
        last_day = q.get("date") or ""
        prev = (q.get("price") if last_day and last_day < today.strftime("%Y%m%d") else q.get("prev_close"))             or o.get("ref_price") or o.get("limit_price")
        if not prev:
            continue
        etf = is_etf(o["code"])
        if o["action"] == "BUY":
            lim = sizer.round_tick(min(prev * 1.03, prev * 1.099), etf, up=False)
        else:
            lim = sizer.round_tick(max(prev * 0.97, prev * 0.901), etf, up=True)
        out.append({"code": o["code"], "name": o.get("name"), "action": o["action"], "shares": o["shares"],
                    "report_limit": o.get("limit_price"), "open_limit": lim, "prev_close": prev,
                    "odd": o["shares"] % 1000 != 0,
                    "text": (f"{'買進' if o['action'] == 'BUY' else '賣出'} {o['code']} {o.get('name', '')} {o['shares']:,} 股："
                             + (f"開盤價成交請掛 {lim}" if o["shares"] % 1000 == 0 else
                                f"零股 09:10 起才撮合（不是 09:00 開盤價），掛 {lim} 確保第一次撮合就成交")
                             + f"（報告原限價 {o.get('limit_price')}）")})
    return out


def preopen_alerts(now: dt.datetime | None = None) -> list[dict]:
    now = now or now_tw()
    if not is_trading_day(now.date()) or not ((8, 40) <= (now.hour, now.minute) < (9, 0)):
        return []
    orders, src = todays_orders(now.date())
    stock = [o for o in orders if o.get("asset", "STOCK") in (None, "STOCK", "ETF")]
    if not stock:
        return []
    lines = open_limits(stock, now.date())
    return [{"key": f"preopen-{now.date()}", "level": "注意", "title": f"開盤前請預掛 {len(lines)} 筆建議單（{src}）",
             "body": "；".join(x["text"] for x in lines)
                     + "。回測：開盤成交的年化約為收盤才成交的 2.5 倍；集合競價以開盤價撮合，限價放寬不會買更貴"}]


def run() -> dict:
    from src.agents import shadow

    today = now_tw().date()
    orders, src = todays_orders(today)
    sh = shadow.run()
    return {"today": today.isoformat(), "source": src, "orders": open_limits(orders) if orders else [],
            "timing_cost": next((b["amount"] for b in sh.get("breakdown", []) if b["name"].startswith("成交時機")), None),
            "worst_fills": sh.get("worst_fills", [])}
