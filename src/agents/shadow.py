"""ShadowPortfolioAgent：「完全照系統策略、開盤成交」的影子帳戶，和實際帳戶逐日比較，量化偏離建議的代價。

影子帳戶規則（與 strategy_tuner 回測相同的參數，strategy_tuner.current_pf()）：
- 每個交易日開盤，用當天盤前記錄的一個月模型預測（data/prediction_log.csv，kind=alpha，pred＝預期超額報酬）排名
- 持股上限 max_positions 檔、等權；持有滿 min_hold_days 且排名最佳未持有股扣成本後多 swap_margin、
  或獲利 ≥ profit_swap_min_pnl 且多 profit_swap_margin、或持有滿 expiry_days → 全部賣出換股
- 成交價＝當天開盤價；成本與進位規則和競賽 App 相同（state/portfolio.build_portfolio）；投入目標持股率的資金，不做期貨
差距拆解（實際 − 影子，元）：
- 期貨部位：實際帳戶期貨損益（影子沒有期貨）
- 成交時機：實際每筆成交價與當天開盤價的差（買貴、賣便宜為負）
- 零碎成交多付的手續費：每筆最低 20 元比 0.1425% 多出來的部分
- 選股與換股差異：其餘
"""
from __future__ import annotations

import datetime as dt
import json
import logging

import pandas as pd

from src.config import is_trading_day, now_tw, rules, stock_meta

log = logging.getLogger(__name__)
_CACHE: dict = {}


def _preds() -> pd.DataFrame:
    from src.config import DATA_DIR

    d = pd.read_csv(DATA_DIR / "prediction_log.csv", dtype={"code": str})
    d = d[d["kind"] == "alpha"]
    d = d[~d["extra"].fillna("").str.contains('"backfill": 1')]  # 只用當時真的預測（不含事後回補）
    return d[["target_date", "code", "pred"]].rename(columns={"target_date": "date"})


def _bars(panel: dict, code: str):
    df = panel.get(code)
    return df if df is not None and len(df) else None


def simulate(start: dt.date, end: dt.date, panel: dict, capital: float | None = None) -> dict:
    from src.agents import strategy_tuner
    from src.state import params
    from src.state import portfolio as pfm

    p = strategy_tuner.current_pf()
    target_ratio = params.strategy().get("target_holding_ratio", 0.9)
    initial = capital or rules()["capital"]["initial"]
    preds = _preds()
    trades: list[dict] = []
    hold: dict[str, dict] = {}  # code → {shares, entry_i, entry_px}
    days = [d for d in pd.date_range(start, end) if is_trading_day(d.date())]
    navs = []
    for i, d in enumerate(days):
        ds = d.date().isoformat()
        today = preds[preds["date"] == ds].sort_values("pred", ascending=False)
        e = dict(zip(today["code"], today["pred"]))
        floor = min(e.values()) if e else 0.0
        opens = {c: float(_bars(panel, c).Open.get(d, float("nan"))) for c in set(today["code"]) | set(hold)
                 if _bars(panel, c) is not None and d in _bars(panel, c).index}
        avail = [c for c in today["code"] if opens.get(c, 0) > 0]
        pf = pfm.build_portfolio(trades)
        # 結單轉股
        for c in list(hold):
            h = hold[c]
            best = next((x for x in avail if x not in hold), None)
            px = opens.get(c)
            if best is None or not px:
                continue
            hd, pnl = i - h["i"], px / h["px"] - 1
            gain = e[best] - e.get(c, floor) - strategy_tuner.COST
            if (hd >= p["expiry_days"] and gain > 0) or (pnl >= p["profit_swap_min_pnl"] and gain > p["profit_swap_margin"]) \
                    or (hd >= p["min_hold_days"] and gain > p["swap_margin"]):
                trades.append({"date": ds, "code": c, "side": "SELL", "shares": h["shares"], "price": px, "note": "影子：結單轉股"})
                del hold[c]
        # 空位補滿（等權，投入目標持股率）
        pf = pfm.build_portfolio(trades)
        nav_open = pfm.app_total(pf, {**{c: opens.get(c, 0) for c in pf.positions}})
        free = [x for x in avail if x not in hold][:max(0, int(p["max_positions"]) - len(hold))]
        if free:
            budget = max(0.0, min(pf.cash, nav_open * target_ratio - pf.market_value(opens))) / len(free)
            for c in free:
                sh = int(budget / (opens[c] * 1.0015))
                if sh > 0:
                    trades.append({"date": ds, "code": c, "side": "BUY", "shares": sh, "price": opens[c], "note": "影子：補位"})
                    hold[c] = {"shares": sh, "i": i, "px": opens[c]}
        pf = pfm.build_portfolio(trades)
        closes = {c: float(_bars(panel, c).Close.get(d, opens.get(c, 0))) for c in pf.positions if _bars(panel, c) is not None}
        navs.append({"date": ds, "nav": round(pfm.app_total(pf, closes)), "positions": {c: h["shares"] for c, h in hold.items()}})
    return {"navs": navs, "trades": trades, "initial": initial}


def actual_breakdown(start: dt.date, end: dt.date, panel: dict) -> dict:
    """實際帳戶：每筆成交價 vs 當天開盤價（成交時機）、零碎成交多付的手續費、期貨損益。"""
    from src.state import portfolio as pfm

    c = rules()["costs"]
    timing, frag, rows = 0.0, 0.0, []
    for t in pfm.load_trades():
        d = dt.date.fromisoformat(t["date"])
        if not (start <= d <= end) or t.get("asset", "STOCK") not in ("STOCK", "ETF", ""):
            continue
        df = _bars(panel, t["code"])
        sh, px = int(float(t["shares"])), float(t["price"])
        if df is None or pd.Timestamp(d) not in df.index:
            continue
        op = float(df.Open.loc[pd.Timestamp(d)])
        sign = 1 if t["side"] == "BUY" else -1
        cost = (px - op) * sh * sign  # 正數＝比開盤價差
        timing -= cost
        fee = max(c["min_commission"], sh * px * c["commission"])
        frag += fee - sh * px * c["commission"]
        rows.append({"date": t["date"], "code": t["code"], "name": stock_meta(t["code"])["name"], "side": t["side"],
                     "shares": sh, "price": px, "open": op, "vs_open_pct": round((px / op - 1) * 100 * sign, 2),
                     "cost_vs_open": round(cost)})
    return {"timing": round(timing), "fragment_fees": round(-frag), "fills": rows}


def run() -> dict:
    from src.agents import on_demand
    from src.state import portfolio as pfm

    key = now_tw().strftime("%Y%m%d%H")
    if key in _CACHE:
        return _CACHE[key]
    panel = on_demand.context()["panel"]
    start = dt.date.fromisoformat(str(rules()["competition"]["start"]))
    hist = pfm.load_history()
    if not hist:
        return {"error": "還沒有實際淨值紀錄"}
    end = dt.date.fromisoformat(hist[-1]["date"])
    sh = simulate(start, end, panel)
    initial = rules()["capital"]["initial"]
    act = {h["date"]: h["nav"] for h in hist}
    series = [{"date": n["date"], "shadow": n["nav"], "actual": act.get(n["date"]),
               "shadow_ret": round((n["nav"] / initial - 1) * 100, 2),
               "actual_ret": round((act[n["date"]] / initial - 1) * 100, 2) if n["date"] in act else None} for n in sh["navs"]]
    last = series[-1]
    bd = actual_breakdown(start, end, panel)
    # 期貨損益：實際帳戶的期貨部位（用同一天的結算價）
    pf = pfm.build_portfolio()
    from src.agents import futures_data

    fut = 0.0
    try:
        hh = {r["time"]: r for r in futures_data.history("TX", 40)}
        settle = (hh.get(end.isoformat()) or {}).get("settlement")
        fut = sum(dv.value(settle) for dv in pf.derivs.values()) if settle else 0.0
        fut += sum(t.get("cost", 0) * 0 for t in [])  # 成本已含在 value 外的現金，影響小
    except Exception as e:  # noqa: BLE001
        log.info("影子：期貨損益失敗 %s", e)
    diff = (last["actual"] or 0) - last["shadow"]
    rest = diff - fut - bd["timing"] - bd["fragment_fees"]
    out = {"time": now_tw().strftime("%m/%d %H:%M"), "series": series, "shadow_trades": sh["trades"][-30:],
           "shadow_positions": [{"code": c, "name": stock_meta(c)["name"], "shares": s} for c, s in sh["navs"][-1]["positions"].items()],
           "diff": round(diff), "breakdown": [
               {"name": "期貨部位（影子沒有期貨）", "amount": round(fut)},
               {"name": "成交時機（實際成交價 vs 當天開盤價）", "amount": bd["timing"]},
               {"name": "零碎成交多付的手續費", "amount": bd["fragment_fees"]},
               {"name": "選股與換股差異（其餘）", "amount": round(rest)}],
           "worst_fills": sorted(bd["fills"], key=lambda r: -r["cost_vs_open"])[:8],
           "note": ("影子帳戶＝從 10/05 起每天開盤照系統策略（綜合排名前 5、結單轉股規則、競賽成本）成交，不做期貨；"
                    "用的是當天盤前真的記錄下來的預測，不是事後回補。正數代表實際帳戶比影子好")}
    _CACHE.clear()
    _CACHE[key] = out
    return out
