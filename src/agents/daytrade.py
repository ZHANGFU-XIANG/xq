"""DayTradeAgent：現股當沖列表、排行與建議（全部規則化，不經 LLM）。

流程：
1. 可當沖名單：證交所 TWTB4U＋櫃買 tpex_securities（含「暫停先賣後買」註記），6 小時快取
2. 日線分數（每個目標交易日算一次）：波動（ATR%）、流動性（20 日均成交值）、
   近 60 日「開盤買、收盤賣」扣成本後的勝率與平均、短線趨勢 → 挑前 N 檔
3. 即時判斷（證交所 MIS，盤中每次查詢重算）：站上開盤價與昨收、估計量比、在今日高低區間的位置
   → 「可以當沖做多／可以當沖做空（先賣後買）／等突破再進／等回檔再進」，附進場、停損、目標、張數、扣成本後損益
4. 日線回測：每天取日線分數前 N 名「開盤買、收盤賣」扣成本的實際結果，誠實呈現有沒有優勢
   （盤中訊號需要分鐘資料，無法用日線回測，畫面上會標註）

成本用真實市場規則（不套競賽規則，config/settings.yaml 的 daytrade.costs）：現股當沖證交稅減半 0.15%（ETF 0.1%）
＋買賣手續費各 0.1425%×折扣 → 無折扣來回約 0.435%。
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import time

import numpy as np
import pandas as pd

from src.agents import alerts, sizer
from src.config import is_trading_day, now_tw, rules, stock_meta
from src.state import portfolio as pfm

log = logging.getLogger(__name__)
SESSION_MIN = 270  # 09:00～13:30


def cfg() -> dict:
    from src.state import params  # 策略調參 Agent 寫入的覆寫值（config/learned_params.yaml）

    return params.get("daytrade")


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


# ---------------------------------------------------------------- 可當沖名單
_ELIG: dict = {"t": 0.0, "v": None}


def eligible() -> dict[str, dict]:
    """{code: {"name", "market", "short_ok"}}；short_ok=False 代表暫停先賣後買（只能先買後賣）。
    兩個來源都失敗時回傳空 dict，呼叫端改用流動性名單並標註。"""
    if _ELIG["v"] is not None and time.time() - _ELIG["t"] < 6 * 3600:
        return _ELIG["v"]
    import requests

    out: dict[str, dict] = {}
    try:
        for x in requests.get("https://openapi.twse.com.tw/v1/exchangeReport/TWTB4U", timeout=20).json():
            if x.get("Code"):
                out[x["Code"].strip()] = {"name": (x.get("Name") or "").strip(), "market": "上市",
                                          "short_ok": (x.get("Suspension") or "").strip() != "Y"}
    except Exception as e:  # noqa: BLE001
        log.warning("證交所可當沖名單失敗：%s", e)
    try:
        for x in requests.get("https://www.tpex.org.tw/openapi/v1/tpex_securities", timeout=30).json():
            c = (x.get("證券代號") or "").strip()
            if c:
                out[c] = {"name": (x.get("證券名稱") or "").strip(), "market": "上櫃",
                          "short_ok": not (x.get("暫停現股賣出後現款買進當沖註記") or "").strip()}
    except Exception as e:  # noqa: BLE001
        log.warning("櫃買可當沖名單失敗：%s", e)
    if out:
        _ELIG.update(t=time.time(), v=out)
    return out


# ---------------------------------------------------------------- 日線分數與回測
def _clip(x, lo=0.0, hi=1.0):
    return np.clip(x, lo, hi)


def _cost_pct(code: str) -> float:
    k = cfg()["costs"]
    return (2 * k["commission"] * k["fee_discount"] + (k["etf_tax"] if pfm.is_etf(code) else k["stock_tax"])) * 100


def _features(panel: dict[str, pd.DataFrame], codes: list[str], target: dt.date, n: int) -> dict[str, pd.DataFrame]:
    """寬表特徵（列＝日期、欄＝代號），只用 target 之前的日 K。"""
    c = cfg()
    cols = {k: {} for k in ("Open", "High", "Low", "Close", "Volume")}
    for code in codes:
        df = panel.get(code)
        if df is None or df.empty:
            continue
        df = df[df.index < pd.Timestamp(target)].tail(n)
        for k in cols:
            cols[k][code] = df[k]
    O, H, L, C, V = (pd.DataFrame(cols[k]).sort_index() for k in ("Open", "High", "Low", "Close", "Volume"))
    pc = C.shift(1)
    tr = pd.concat([(H - L), (H - pc).abs(), (L - pc).abs()]).groupby(level=0).max().reindex(C.index)
    atr = tr.rolling(14).mean()
    atr_pct = atr / C * 100
    val20 = (C * V).rolling(20).mean()
    vol20 = V.rolling(20).mean()
    ma5, ma20 = C.rolling(5).mean(), C.rolling(20).mean()
    cost = pd.Series({k: _cost_pct(k) for k in C.columns})
    oc = (C / O - 1) * 100
    hd = c["history_days"]
    oc_mean = oc.rolling(hd).mean()
    win = oc.gt(cost, axis=1).astype(float).where(oc.notna()).rolling(hd).mean()
    oh_mean = ((H / O - 1) * 100).rolling(hd).mean()
    trend = pd.DataFrame(np.where((C > ma5) & (ma5 > ma20), 1.0, np.where(C > ma20, 0.6, 0.2)), index=C.index, columns=C.columns)
    # 當沖適合度＝波動夠大（扣成本後有空間）＋流動性好（進出不被價差吃掉）。
    # 2026-10-12 研究：近一年歷史開盤買收盤賣勝率、多頭排列、近 5 日漲幅越高，隔天盤中反而越差（分五組單調遞減），所以不放進分數，只顯示
    # 波動 1.5%→7.5%、成交值 1 億→300 億線性給分（區間太窄會讓前段班全部滿分、排名變成照代號排）
    score = 55 * _clip((atr_pct - 1.5) / 6) + 45 * _clip(np.log10((val20 / 1e8).clip(lower=1e-6)) / math.log10(300))
    ok = (C >= c["min_price"]) & (val20 >= c["min_avg_value20"]) & (atr_pct >= c["min_atr_pct"])
    return {"O": O, "H": H, "L": L, "C": C, "atr": atr, "atr_pct": atr_pct, "val20": val20, "vol20": vol20,
            "ma5": ma5, "ma20": ma20, "oc": oc, "oc_mean": oc_mean, "win": win, "oh_mean": oh_mean,
            "trend": trend, "score": score.where(ok), "cost": cost}


def _signal_returns(f: dict[str, pd.DataFrame], side: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """用日 K 近似盤中訊號：做多＝價格站上 max(開盤, 昨收) 一檔以上才進場，做空＝跌破 min(開盤, 昨收)；
    停損＝max(stop_atr×ATR%, min_stop_pct)、目標＝reward_risk 倍停損，都沒碰到就收盤出場。
    日 K 不知道當天先碰停損還是目標，所以回傳「先停損（保守）」與「先停利（樂觀）」兩種結果（扣來回成本，%）。"""
    c = cfg()
    O, H, L, C = f["O"], f["H"], f["L"], f["C"]
    pc = C.shift(1)
    s = (c["stop_atr"] * f["atr_pct"].shift(1)).clip(lower=c["min_stop_pct"]) / 100
    if side == "long":
        e = np.maximum(O, pc) * 1.001
        hit = H >= e
        hs, ht, close = L <= e * (1 - s), H >= e * (1 + c["reward_risk"] * s), C / e - 1
    else:
        e = np.minimum(O, pc) * 0.999
        hit = L <= e
        hs, ht, close = H >= e * (1 + s), L <= e * (1 - c["reward_risk"] * s), e / C - 1
    rr = c["reward_risk"] * s
    worst = (-s).where(hs, rr.where(ht, close))
    best = rr.where(ht, (-s).where(hs, close))
    return (worst * 100).sub(f["cost"], axis=1).where(hit), (best * 100).sub(f["cost"], axis=1).where(hit), hit


def _backtest(f: dict[str, pd.DataFrame]) -> dict | None:
    """日線回測（每天只用前一日收盤後就知道的日線分數挑前 prefilter_n 名）：
    1. 前 top_n 名開盤買、收盤賣；2. 盤中做多訊號；3. 盤中做空訊號。全部扣來回成本。"""
    c = cfg()
    score = f["score"].shift(1)
    days = score.index[-c["backtest_days"]:]
    if len(days) < 60:
        return None
    rank = score.loc[days].rank(axis=1, ascending=False)
    pool = rank <= c["prefilter_n"]
    top = rank <= c["top_n"]
    oc = f["oc"].sub(f["cost"], axis=1).loc[days]

    def summ(x: pd.DataFrame, mask: pd.DataFrame) -> dict | None:
        v = x.loc[days].where(mask).stack().dropna()
        if len(v) < 50:
            return None
        return {"trades": int(len(v)), "avg_net_pct": round(float(v.mean()), 3), "win_rate": round(float((v > 0).mean()), 3)}

    out = {"from": str(days[0].date()), "to": str(days[-1].date()), "days": len(days),
           "open_close": summ(oc, top), "baseline": summ(oc, score.loc[days].notna())}
    for side in ("long", "short"):
        w, b, _ = _signal_returns(f, side)
        sw, sb = summ(w, pool), summ(b, pool)
        if sw and sb:
            mid = (sw["avg_net_pct"] + sb["avg_net_pct"]) / 2
            out[side] = {"worst": sw, "best": sb, "avg_net_pct": round(mid, 3), "edge": bool(mid > 0)}
        else:
            out[side] = None
    out["method"] = (f"每天只用前一日收盤後的日線分數挑前 {c['prefilter_n']} 名；做多訊號＝站上開盤價與昨收、做空＝跌破開盤價與昨收，"
                     f"停損 {c['stop_atr']} 倍 ATR（至少 {c['min_stop_pct']}%）、目標 {c['reward_risk']} 倍停損、否則收盤出場；"
                     "日 K 無法知道停損與目標誰先到，取「先停損」與「先停利」的平均；全部扣來回成本。量比條件需要分鐘資料，未納入回測")
    return out


_DAILY: dict = {}


def daily(refresh: bool = False) -> dict:
    """日線部分（同一目標交易日只算一次）：{target, rows: {code: {...}}, ranked: [code], backtest, source}。"""
    from src.agents import on_demand

    target = on_demand.current_target()
    hit = _DAILY.get(target)
    if hit and not refresh:
        return hit
    ctx = on_demand.context()
    elig = eligible()
    codes = [c for c in ctx["codes"] if not elig or c in elig]
    f = _features(ctx["panel"], codes, target, cfg()["backtest_days"] + cfg()["history_days"] + 30)
    last = {k: v.iloc[-1] for k, v in f.items() if isinstance(v, pd.DataFrame)}
    rows = {}
    for code in f["C"].columns:
        close = last["C"].get(code)
        if close is None or np.isnan(close):
            continue
        m = elig.get(code) or {}
        tr = last["trend"].get(code)
        rows[code] = {
            "code": code, "name": stock_meta(code).get("name") or m.get("name") or code,
            "market": m.get("market") or ("上櫃" if stock_meta(code).get("market") == "TPEx" else "上市"),
            "short_ok": bool(m.get("short_ok", False)), "is_etf": pfm.is_etf(code),
            "close": round(float(close), 2), "atr": _num(last["atr"].get(code)), "atr_pct": _num(last["atr_pct"].get(code)),
            "val20_e8": _num(last["val20"].get(code) / 1e8 if last["val20"].get(code) else None),
            "vol20_lots": _num(last["vol20"].get(code) / 1000 if last["vol20"].get(code) else None, 0),
            "win60": _num(last["win"].get(code), 3), "oc_mean60": _num(last["oc_mean"].get(code), 3),
            "oh_mean60": _num(last["oh_mean"].get(code)),
            "trend": "多頭排列" if tr == 1.0 else "站上月線" if tr == 0.6 else "月線之下",
            "base_score": _num(last["score"].get(code), 1), "cost_pct": round(float(f["cost"][code]), 3),
            "data_date": str(f["C"][code].dropna().index[-1].date()),
            "prev_high": _num(last["H"].get(code)), "prev_low": _num(last["L"].get(code)),
            "ma5": _num(last["ma5"].get(code)), "ma20": _num(last["ma20"].get(code)),
        }
    ranked = sorted((c for c, r in rows.items() if r["base_score"] is not None), key=lambda c: -rows[c]["base_score"])
    out = {"target": target, "rows": rows, "ranked": ranked, "backtest": _backtest(f),
           "source": "證交所／櫃買中心可當沖名單" if elig else "無法取得可當沖名單，暫用流動性名單（下單前請確認可當沖）"}
    _DAILY.clear()
    _DAILY[target] = out
    return out


def _num(x, nd: int = 2):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else round(x, nd)


# ---------------------------------------------------------------- 即時判斷
def phase(now: dt.datetime | None = None) -> tuple[str, str]:
    now = now or now_tw()
    c = cfg()
    if not is_trading_day(now.date()):
        return "休市", "今天休市；下方為下一個交易日的當沖觀察清單（依日線分數）"
    hm = now.hour * 60 + now.minute
    if hm < 540:
        return "盤前", "尚未開盤；開盤後依即時價判斷能不能當沖"
    if c["open_wait_min"] and hm < 540 + c["open_wait_min"]:
        return "開盤觀察", f"開盤前 {c['open_wait_min']} 分鐘波動大、方向不明，先不開新倉，看清楚再進"
    if hm < _hm(c["last_entry"]):
        return "盤中", f"即時判斷中；{c['last_entry']} 後不開新倉，{c['flat_by']} 前平倉"
    if hm <= 810:
        return "收盤前", f"{c['last_entry']} 後不開新倉，{c['flat_by']} 前平倉"
    return "收盤後", "今日已收盤；下方為下一個交易日的當沖觀察清單（依日線分數）"


def _account(rt_held: dict) -> tuple[float, float]:
    """當沖資金與可用現金（張數上限用）：有設定 daytrade.capital 就用它，否則用系統帳戶總資產；失敗時退回初始資金。"""
    if cfg().get("capital"):
        cap = float(cfg()["capital"])
        return cap, cap
    try:
        pf = pfm.build_portfolio()
        prices = {c: (rt_held.get(c) or {}).get("price") or p.avg_cost for c, p in pf.positions.items()}
        return float(pf.nav(prices)), float(pf.cash)
    except Exception as e:  # noqa: BLE001
        log.warning("當沖讀取帳戶失敗：%s", e)
        cap = rules()["capital"]
        return float(cap["initial"]), float(cap["securities"])


def _size(code: str, entry: float, stop: float, nav: float, cash: float) -> dict:
    """依「觸停損最多賠總資產 risk_per_trade」與部位上限算張數；不足一張改零股。"""
    c = cfg()
    per_share = abs(entry - stop) + entry * _cost_pct(code) / 100
    by_risk = nav * c["risk_per_trade"] / per_share if per_share > 0 else 0
    cap = nav * c["max_notional_pct"]
    shares = int(min(by_risk, cap / entry))
    lots = shares // 1000
    shares = lots * 1000 if lots else shares
    note = "不足一張，改用盤中零股（09:10～13:30）" if shares and not lots else None
    return {"shares": shares, "qty_text": sizer.fmt_shares(shares) if shares else "0 股", "notional": round(entry * shares), "size_note": note}


def _pnl(code: str, shares: int, entry: float, exit_: float, side: str) -> int:
    """扣手續費與證交稅後的損益（元）；side=long 先買後賣，short 先賣後買。"""
    if not shares:
        return 0
    buy_px, sell_px = (entry, exit_) if side == "long" else (exit_, entry)
    k = cfg()["costs"]
    buy_amt, sell_amt = math.floor(shares * buy_px + 1e-6), math.floor(shares * sell_px + 1e-6)
    fee = lambda amt: max(k["min_fee"], math.floor(amt * k["commission"] * k["fee_discount"] + 1e-9))  # noqa: E731
    tax = math.floor(sell_amt * (k["etf_tax"] if pfm.is_etf(code) else k["stock_tax"]) + 1e-9)
    return int(sell_amt - buy_amt - fee(buy_amt) - fee(sell_amt) - tax)


LEVELS_NOTE = ("技術價位（前一日樞紐點、前日高低、均線）只供掛單參考，未經分鐘資料驗證：2025-10～2026-10 日 K 回測，"
               "掛支撐價買進後抱到收盤，S1 每筆 −0.51%、S2 −0.25%、前日低點 −0.59%（已扣成本），所以買到支撐價就要在賣點或收盤前出場，"
               "不可留倉；沒有方法能保證買在絕對低點")


def levels(d: dict) -> dict | None:
    """前一日樞紐點（P、S1、S2、R1、R2）與前日高低、5／20 日線 → 買點區間、賣點區間、停損參考。"""
    h, l_, c = d.get("prev_high"), d.get("prev_low"), d.get("close")
    if not (h and l_ and c):
        return None
    etf = d.get("is_etf", False)
    pv = (h + l_ + c) / 3
    s1, r1 = 2 * pv - h, 2 * pv - l_
    s2, r2 = pv - (h - l_), pv + (h - l_)
    t = lambda x, up=False: sizer.round_tick(x, etf, up=up)  # noqa: E731
    buy1 = t(max(s1, l_))          # 第一買點：S1 與前日低點較高者
    buy2 = t(min(s1, s2, l_))      # 第二買點（較深的支撐）
    sell1 = t(pv, up=True)         # 第一賣點：樞紐點
    sell2 = t(max(r1, h), up=True)  # 第二賣點：R1 與前日高點較高者
    stop = t(buy2 - 0.5 * (d.get("atr") or 0))
    return {"pivot": round(pv, 2), "s1": t(s1), "s2": t(s2), "r1": t(r1, up=True), "r2": t(r2, up=True),
            "prev_high": h, "prev_low": l_, "ma5": d.get("ma5"), "ma20": d.get("ma20"),
            "buy_zone": [buy2, buy1], "sell_zone": [sell1, sell2], "stop": stop,
            "text": (f"買點參考：{buy1}（S1／前日低點）、更低接 {buy2}；賣點參考：{sell1}（樞紐點）、{sell2}（R1／前日高點）；"
                     f"跌破 {stop} 停損；5 日線 {d.get('ma5')}、月線 {d.get('ma20')}"),
            "note": LEVELS_NOTE}


def evaluate(d: dict, q: dict | None, ph: str, now: dt.datetime, nav: float, cash: float, bt: dict | None = None) -> dict:
    """單檔當沖判斷。d：日線列；q：即時報價；bt：日線回測（require_edge 時，訊號的同規則回測沒有優勢就不給進場）。"""
    c = cfg()
    etf = d["is_etf"]
    row = {**d, "levels": levels(d), "price": None, "chg_pct": None, "from_open_pct": None, "volume_ratio": None, "range_pos": None,
           "direction": None, "entry": None, "stop": None, "target": None, "reasons": [], "plan": None}
    stop_pct = max(c["stop_atr"] * (d["atr_pct"] or 0), c["min_stop_pct"])
    hist = (f"近 {c['history_days']} 日開盤買收盤賣扣成本勝率 {d['win60'] * 100:.0f}%、平均 {d['oc_mean60']:+.2f}%"
            if d.get("win60") is not None and d.get("oc_mean60") is not None else None)
    base_reasons = [f"日均波動 {d['atr_pct']:.1f}%（來回成本 {d['cost_pct']:.2f}%）",
                    f"20 日均成交值 {d['val20_e8']:.0f} 億", d["trend"]] + ([hist] if hist else [])
    if q:
        price, op, hi, lo, prev = q.get("price"), q.get("open"), q.get("high"), q.get("low"), q.get("prev_close")
        row.update(price=price, chg_pct=q.get("chg_pct"), open=op, high=hi, low=lo, prev_close=prev,
                   limit_up=q.get("limit_up"), limit_down=q.get("limit_down"), quote_time=q.get("time"))
    live = q and ph in ("開盤觀察", "盤中", "收盤前") and q.get("price") and q.get("open")
    if not live:
        row.update(verdict="開盤後觀察", score=d["base_score"], reasons=base_reasons,
                   plan=((f"開盤 {c['open_wait_min']} 分鐘後，" if c["open_wait_min"] else "開盤後")
                         + f"股價站上開盤價與昨收、估計量比 ≥ {c['min_volume_ratio']} 做多（跌破開盤價與昨收可做空）；"
                         f"停損約 {stop_pct:.1f}%，目標約 {stop_pct * c['reward_risk']:.1f}%，{c['flat_by']} 前平倉"))
        return row
    minutes = max(5, min(SESSION_MIN, now.hour * 60 + now.minute - 540))
    vr = q.get("volume_lots") / (d["vol20_lots"] * minutes / SESSION_MIN) if q.get("volume_lots") and d.get("vol20_lots") else None
    from_open = (price / op - 1) * 100
    rng = (hi - lo) if hi and lo else 0
    pos = (price - lo) / rng if rng > 0 else 0.5
    row.update(from_open_pct=round(from_open, 2), volume_ratio=_num(vr), range_pos=round(pos, 2))
    v = vr or 0
    live_long = (0.35 * min(v / 2.5, 1) + 0.35 * min(max(from_open, 0) / 2.5, 1) + 0.3 * pos) if from_open > 0 else 0.15 * pos
    live_short = (0.35 * min(v / 2.5, 1) + 0.35 * min(max(-from_open, 0) / 2.5, 1) + 0.3 * (1 - pos)) if from_open < 0 else 0.15 * (1 - pos)
    if not d["short_ok"]:
        live_short = 0.0
    row["score"] = round(0.5 * d["base_score"] + 50 * max(live_long, live_short), 1)
    vr_txt = f"估計量比 {v:.1f}" if vr is not None else "量比無資料"
    live_reasons = [f"現價 {price}（{q.get('chg_pct'):+.2f}%），距開盤 {from_open:+.2f}%", vr_txt,
                    f"位在今日高低區間 {pos * 100:.0f}% 位置"]
    if q.get("at_limit_up"):
        row.update(verdict="等打開再進", reasons=["已漲停鎖住：現在買不到"] + live_reasons,
                   plan=f"漲停打開、站穩開盤價 {op} 上方且估計量比 ≥ {c['min_volume_ratio']} 再做多")
        return row
    if q.get("at_limit_down"):
        row.update(verdict="等打開再進", reasons=["已跌停鎖住：現在掛單不容易成交"] + live_reasons,
                   plan=f"跌停打開、站回開盤價 {op} 上方再做多")
        return row
    long_ok = price > op and price > prev and pos >= 0.6 and v >= c["min_volume_ratio"]
    short_ok = d["short_ok"] and price < op and price < prev and pos <= 0.4 and v >= c["min_volume_ratio"]
    side = "long" if long_ok else "short" if short_ok else None
    if ph == "收盤前":
        row.update(verdict="準備平倉", reasons=live_reasons,
                   plan=f"{c['last_entry']} 後不開新倉；已有這檔的當沖部位，{c['flat_by']} 前不論賺賠全部平倉")
        return row
    tick = sizer.tick_size(price, etf)
    if side is None:
        missing = []
        if price <= op:
            missing.append(f"現價未站上開盤價 {op}")
        if price <= prev:
            missing.append(f"現價未站上昨收 {prev}")
        if pos < 0.6:
            missing.append("還沒走到今日區間上緣")
        if v < c["min_volume_ratio"]:
            missing.append(f"{vr_txt}，低於 {c['min_volume_ratio']}")
        trigger = sizer.round_tick(max(hi or price, op, prev) + tick, etf, up=True)
        row.update(verdict="等突破再進", reasons=missing + base_reasons[:2], trigger=trigger,
                   plan=f"站上 {trigger} 且估計量比 ≥ {c['min_volume_ratio']} 再做多")
        return row
    dist = max(c["stop_atr"] * (d["atr"] or 0), price * c["min_stop_pct"] / 100)
    entry = price
    if side == "long":
        stop = sizer.round_tick(entry - dist, etf)
        target = sizer.round_tick(entry + c["reward_risk"] * (entry - stop), etf)
        if q.get("limit_up"):
            target = min(target, sizer.round_tick(q["limit_up"] - tick, etf))
    else:
        stop = sizer.round_tick(entry + dist, etf, up=True)
        target = sizer.round_tick(entry - c["reward_risk"] * (stop - entry), etf, up=True)
        if q.get("limit_down"):
            target = max(target, sizer.round_tick(q["limit_down"] + tick, etf, up=True))
    net_reward = (abs(target - entry) / entry * 100) - d["cost_pct"]
    if net_reward <= 0:
        back_px = sizer.round_tick(entry - dist, etf) if side == "long" else sizer.round_tick(entry + dist, etf, up=True)
        row.update(verdict="等回檔再進", trigger=back_px,
                   reasons=[f"離{'漲' if side == 'long' else '跌'}停太近，現價進場到目標價扣成本只剩 {net_reward:.2f}%"] + live_reasons,
                   plan=(f"拉回到 {back_px} 附近再做多" if side == "long" else f"反彈到 {back_px} 附近再做空"))
        return row
    sz = _size(d["code"], entry, stop, nav, cash)
    win, loss = _pnl(d["code"], sz["shares"], entry, target, side), _pnl(d["code"], sz["shares"], entry, stop, side)
    verb = "買進" if side == "long" else "先賣出（先賣後買）"
    back = "賣出" if side == "long" else "買回"
    sbt = (bt or {}).get(side)
    gated = c.get("require_edge", True) and not (sbt and sbt["edge"])
    side_txt = "做多" if side == "long" else "做空"
    gate_reason = (f"{side_txt}訊號成立，但同規則近一年回測扣成本平均 {sbt['avg_net_pct']:+.2f}%"
                   f"（勝率約 {(sbt['worst']['win_rate'] + sbt['best']['win_rate']) * 50:.0f}%），沒有優勢" if sbt
                   else f"{side_txt}訊號成立，但回測資料不足，無法證實有優勢")
    row.update(verdict=("先不要當沖" if gated else "可以當沖做多" if side == "long" else "可以當沖做空"), direction=side,
               signal=f"{side_txt}訊號成立", gated=gated,
               entry=entry, stop=stop, target=target, **sz,
               net_reward_pct=round(net_reward, 2), net_risk_pct=round(abs(entry - stop) / entry * 100 + d["cost_pct"], 2),
               profit_at_target=win, loss_at_stop=loss,
               reasons=([gate_reason] if gated else []) + live_reasons + base_reasons[:2],
               plan=("僅供參考（回測沒有優勢，不建議進場）：" if gated else "") + (f"限價 {entry} {verb} {sz['qty_text']}；{'跌破' if side == 'long' else '漲過'} {stop} 立刻{back}停損，"
                     f"到 {target} {back}一半、其餘停損移到進場價；{c['flat_by']} 前不論賺賠全部{back}"))
    return row


_BOARD: dict = {}  # 股價上限 → {"t": 時間, "v": 結果}
ORDER = {"可以當沖做多": 0, "可以當沖做空": 1, "等突破再進": 2, "等回檔再進": 3, "開盤後觀察": 4, "準備平倉": 5,
         "等打開再進": 6, "先不要當沖": 7}


def board(refresh: bool = False, max_price: float | None = None) -> dict:
    """當沖排行（盤中 15 秒快取、盤後 5 分鐘）。max_price：股價上限（元／股，依前一日收盤價），先篩價格再取前 prefilter_n 名。"""
    ttl = 15 if alerts.is_trading_hours() else 300
    cache = _BOARD.get(max_price)
    if cache and not refresh and time.time() - cache["t"] < ttl:
        return cache["v"]
    c = cfg()
    now = now_tw()
    ph, ph_note = phase(now)
    dl = daily()
    ranked = [k for k in dl["ranked"] if not max_price or (dl["rows"][k].get("close") or 0) <= max_price]
    codes = ranked[:c["prefilter_n"]]
    held = list(pfm.build_portfolio().positions)
    rt = alerts.realtime(codes + held) if ph not in ("休市",) else {}
    nav, cash = _account(rt)
    bt = dl["backtest"]
    rows = [evaluate(dl["rows"][code], rt.get(code), ph, now, nav, cash, bt) for code in codes]
    # 訊號成立但被回測擋下（先不要當沖）的，排在「等突破」之前：它們是盤中最強的
    rows.sort(key=lambda r: (min(ORDER.get(r["verdict"], 9), 1.5) if r.get("signal") else ORDER.get(r["verdict"], 9),
                             -(r.get("score") or 0)))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    act = [r for r in rows if r["verdict"].startswith("可以當沖")]
    picks = act or [r for r in rows if r.get("plan")]  # 一定給推薦：沒有訊號成立時，推薦排行前面、有操作計畫的標的
    signals = [r for r in rows if r.get("signal")]
    k = c["costs"]
    disc = "" if k["fee_discount"] >= 1 else f"（手續費 {k['fee_discount'] * 10:g} 折）"
    out = {
        "time": now.strftime("%Y-%m-%d %H:%M:%S"), "phase": ph, "phase_note": ph_note, "target": str(dl["target"]),
        "trading": alerts.is_trading_hours(), "source": dl["source"], "eligible_count": len(eligible()),
        "max_price": max_price, "n_in_range": len(ranked),
        "nav": round(nav), "cash": round(cash),
        "counts": {k: sum(1 for r in rows if r["verdict"] == k) for k in ORDER},
        "top": act[:c["top_n"]], "picks": picks[:c["top_n"]], "signals": signals[:c["top_n"]], "rows": rows, "backtest": bt,
        "edge": {s: bool((bt or {}).get(s) and bt[s]["edge"]) for s in ("long", "short")},
        "cost_note": (f"來回成本{disc}：股票約 {(2 * k['commission'] * k['fee_discount'] + k['stock_tax']) * 100:.3f}%"
                      f"（現股當沖證交稅減半 {k['stock_tax'] * 100:.2f}%＋手續費 {k['commission'] * k['fee_discount'] * 100:.4f}%×2），"
                      f"ETF 約 {(2 * k['commission'] * k['fee_discount'] + k['etf_tax']) * 100:.3f}%；每筆最低手續費 {k['min_fee']} 元；"
                      "折扣可在設定 daytrade.costs.fee_discount 調整"),
        "capital_note": ("當沖資金依設定" if c.get("capital") else "當沖資金＝系統帳戶總資產") + f" {nav:,.0f} 元",
        "rules": [(f"開盤 {c['open_wait_min']} 分鐘內不開新倉，" if c["open_wait_min"] else "") + f"{c['last_entry']} 後不開新倉",
                  f"張數：觸停損約賠當沖資金 {c['risk_per_trade'] * 100:.1f}%；停損 {c['stop_atr']} 倍 ATR（至少 {c['min_stop_pct']}%）、目標 {c['reward_risk']} 倍停損",
                  f"到目標先出一半、其餘停損移到進場價；{c['flat_by']} 前平倉",
                  "做空＝現股先賣後買，只列出未被暫停先賣後買的股票"],
        "note": ("排行＝0.5×當沖適合度（日均波動、20 日均成交值）＋0.5×盤中強弱（距開盤、估計量比、在今日高低區間的位置）。"
                 "估計量比＝今日累計量 ÷（20 日均量 × 已開盤時間比例），開盤初期量通常偏大。"
                 "回測用日 K 近似盤中訊號（量比條件與停損停利先後順序需要分鐘資料，無法精確回測）；所有數字是規則計算，不是保證。"),
    }
    out["verdict"] = _overall(out, bt)
    _BOARD[max_price] = {"t": time.time(), "v": out}
    return out


def _overall(out: dict, bt: dict | None) -> str:
    """整體結論（一句話）：一定給推薦（使用者 2026-10-12 要求不要顯示「先不要當沖」）。"""
    if out["top"]:
        r = out["top"][0]
        return f"可以當沖：第一名 {r['code']} {r['name']}，{r['plan']}"
    if out["picks"]:
        r = out["picks"][0]
        head = "當沖推薦（尚未開盤）" if out["phase"] in ("休市", "盤前", "收盤後") else "當沖推薦"
        return f"{head}：第一名 {r['code']} {r['name']}，{r['plan']}"
    return "當沖推薦：排行計算中，請稍後重新整理"


def stock(code: str) -> dict:
    """單檔當沖判斷（AI 助理、個股頁用）；不在名單時說明原因。"""
    code = code.strip().upper()
    elig = eligible()
    name = stock_meta(code).get("name") or code
    if elig and code not in elig:
        return {"code": code, "name": name, "verdict": "不在當沖名單", "reasons": ["不在證交所／櫃買中心今日可現股當沖名單，規定上不能現股當沖"],
                "plan": "改看當沖排行推薦的標的"}
    dl = daily()
    d = dl["rows"].get(code)
    if d is None:
        return {"code": code, "name": name, "verdict": "資料不足",
                "reasons": ["不在本系統流動性名單（20 日均成交值或歷史資料不足），無法計算進場、停損與目標"],
                "plan": "改看當沖排行推薦的標的"}
    c = cfg()
    gaps = []
    if d["close"] < c["min_price"]:
        gaps.append(f"股價 {d['close']} 低於 {c['min_price']} 元")
    if (d["val20_e8"] or 0) * 1e8 < c["min_avg_value20"]:
        gaps.append(f"20 日均成交值 {d['val20_e8']:.1f} 億，低於 {c['min_avg_value20'] / 1e8:.0f} 億")
    if (d["atr_pct"] or 0) < c["min_atr_pct"]:
        gaps.append(f"日均波動 {d['atr_pct']:.1f}%，低於 {c['min_atr_pct']}%（扣掉來回成本 {d['cost_pct']:.2f}% 後空間太小）")
    now = now_tw()
    ph, ph_note = phase(now)
    rt = alerts.realtime([code]) if ph != "休市" else {}
    nav, cash = _account({})
    if d["base_score"] is None:
        d = {**d, "base_score": 0.0}
    r = evaluate(d, rt.get(code), ph, now, nav, cash, dl["backtest"])
    if gaps:
        r.update(reasons=r["reasons"] + [f"留意：{g}" for g in gaps])
    rank = next((i for i, x in enumerate(dl["ranked"], 1) if x == code), None)
    return {**r, "phase": ph, "phase_note": ph_note, "daily_rank": rank, "daily_total": len(dl["ranked"]),
            "time": now.strftime("%Y-%m-%d %H:%M:%S")}
