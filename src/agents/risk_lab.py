"""RiskLab：壓力測試、期貨追繳點位、VaR、到競賽結束的模擬、黑天鵝指標（全部由程式計算，只提醒、不改建議）。

使用者 2026-10-14 選擇「純報酬最大化」：這裡的結果只在網站「風險儀表板」、報告與通知中提醒，
不會自動降低曝險或改持股建議。

- 壓力情境：加權 −3／−5／−10／−20%（各持股用 β60）、費半 −8%、台幣急貶 2%（用 macro.sensitivities 的因子 β）
- 期貨：維持保證金、追繳點位（大盤再跌多少點期貨權益會低於維持保證金）
- VaR／CVaR：用「現在的部位」套過去 250 個交易日的每日價格變動（歷史模擬法），單日 95%
- 到 11/27：同一份每日損益以 5 日區塊 bootstrap 模擬剩餘交易日（假設部位不變），算最後報酬分布與收正機率
- 黑天鵝指標：每個訊號都用「美股收盤得知、台股隔日開盤才能動作」回測（加權隔日開盤 → 20 日後收盤），
  只有平均報酬明顯較差（差距 ≤ −1%、t ≤ −2、次數 ≥ 20）才標「有預測力」，否則標「僅資訊」
"""
from __future__ import annotations

import datetime as dt
import logging
import time

import numpy as np
import pandas as pd

from src.config import is_trading_day, now_tw, rules, stock_meta

log = logging.getLogger(__name__)
_CACHE: dict = {}
STRESS = [("加權指數 −3%", "TAIEX", -0.03), ("加權指數 −5%", "TAIEX", -0.05), ("加權指數 −10%", "TAIEX", -0.10),
          ("加權指數 −20%（崩盤）", "TAIEX", -0.20), ("費城半導體 −8%", "US_TECH", -0.08), ("台幣急貶 2%", "USD_TWD", 0.02)]
SWAN_TICKERS = ["^VIX", "^VIX3M", "HYG", "JPY=X", "TWD=X"]


def _cached(key: str, ttl: float, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    v = fn()
    _CACHE[key] = (time.time(), v)
    return v


def comp_end() -> dt.date:
    return dt.date.fromisoformat(str(rules().get("competition", {}).get("end", "2026-11-27")))


def trading_days_left(today: dt.date, end: dt.date) -> int:
    n, d = 0, today + dt.timedelta(days=1)
    while d <= end:
        n += is_trading_day(d)
        d += dt.timedelta(days=1)
    return n


# ---------------------------------------------------------------- 部位與價格
def _book():
    from src import pipeline
    from src.agents import alerts, on_demand
    from src.state import portfolio as pfm

    ctx = on_demand.context()
    pf = pfm.build_portfolio()
    rt = alerts.realtime(list(pf.positions))
    prices = {c: (rt.get(c) or {}).get("price") or float(ctx["panel"][c].Close.iloc[-1]) if c in ctx["panel"] else p.avg_cost
              for c, p in pf.positions.items()}
    marked = pipeline.with_marks(pf, prices)
    nav = pfm.app_total(pf, marked)
    return ctx, pf, marked, nav


def stress(ctx, pf, prices: dict, nav: float) -> list[dict]:
    from src.agents import macro, overnight

    codes = list(pf.positions)
    betas = overnight._betas(codes) if codes else {}
    tx = ctx["taiex"].rename(columns=str)
    panel = {**ctx["panel"], "__TAIEX__": tx}
    sens = macro.sensitivities(codes + ["__TAIEX__"], panel, ctx["us_hist"], pd.Timestamp(now_tw().date() + dt.timedelta(days=1)))
    fut = [(c, d) for c, d in pf.derivs.items() if d.kind == "FUT"]
    initial = rules()["capital"]["initial"]
    out = []
    for name, factor, shock in STRESS:
        stock = 0.0
        for c, p in pf.positions.items():
            mv = p.shares * prices.get(c, p.avg_cost)
            b = betas.get(c, 1.0) if factor == "TAIEX" else (sens.get(c, {}).get(factor) or {}).get("beta", 0.0)
            stock += mv * b * shock
        bt = 1.0 if factor == "TAIEX" else (sens.get("__TAIEX__", {}).get(factor) or {}).get("beta", 0.0)
        futures = sum(d.qty * d.multiplier * (prices.get(c) or d.avg_price) * bt * shock for c, d in fut)
        total = stock + futures
        out.append({"scenario": name, "stock": round(stock), "futures": round(futures), "total": round(total),
                    "pct_nav": round(total / nav * 100, 2) if nav else None,
                    "return_after": round(((nav + total) / initial - 1) * 100, 2)})
    return out


def margin_call(pf, prices: dict) -> dict | None:
    from src.agents import futures_data

    fut = [(c, d) for c, d in pf.derivs.items() if d.kind == "FUT" and d.qty]
    if not fut:
        return None
    m = futures_data.margins()
    equity = pf.futures_equity(prices)
    maint = sum(abs(d.qty) * (m.get(d.root) or {}).get("maintenance", 0) for _, d in fut)
    init = sum(abs(d.qty) * (m.get(d.root) or {}).get("initial", 0) for _, d in fut)
    per_point = sum(d.qty * d.multiplier for _, d in fut)  # 多單為正：大盤每跌 1 點權益減少 per_point 元
    c0, d0 = fut[0]
    px = prices.get(c0) or d0.avg_price
    room = (equity - maint) / abs(per_point) if per_point else None
    line = px - room if per_point > 0 and room is not None else px + room if room is not None else None
    return {"equity": round(equity), "maintenance": round(maint), "initial": round(init), "per_point": per_point,
            "price": px, "points_to_call": round(room) if room is not None else None,
            "call_level": round(line) if line is not None else None,
            "pct_to_call": round(room / px * 100, 2) if room is not None and px else None,
            "topup_to_initial": round(max(0.0, init - equity)),
            "text": (f"期貨權益 {equity:,.0f} 元、維持保證金 {maint:,.0f} 元；台指再{'跌' if per_point > 0 else '漲'} "
                     f"{room:,.0f} 點（約 {room / px * 100:.1f}%，到 {line:,.0f}）會被追繳，需補到原始保證金 {init:,.0f} 元"
                     if room is not None else "")}


def _pnl_history(ctx, pf) -> pd.Series:
    """現在的部位套過去每日價格變動 → 每日損益（元）。"""
    idx = ctx["taiex"].Close.dropna().index[-251:]
    total = pd.Series(0.0, index=idx[1:])
    for c, p in pf.positions.items():
        df = ctx["panel"].get(c)
        if df is None or len(df) < 30:
            continue
        diff = df.Close.reindex(idx).ffill().diff().iloc[1:].fillna(0)
        total = total.add(p.shares * diff, fill_value=0)
    tx = ctx["taiex"].Close.reindex(idx).ffill().diff().iloc[1:].fillna(0)
    for d in pf.derivs.values():
        if d.kind == "FUT":
            total = total.add(d.qty * d.multiplier * tx, fill_value=0)
    return total


def _tx_1y(ctx) -> float:
    c = ctx["taiex"].Close.dropna()
    return float(c.iloc[-1] / c.iloc[-251] - 1) * 100 if len(c) > 251 else 0.0


def var_and_paths(ctx, pf, nav: float, days_left: int, n_paths: int = 4000, block: int = 5, seed: int = 7) -> dict:
    pnl = _pnl_history(ctx, pf).to_numpy()
    if len(pnl) < 60 or not nav:
        return {}
    q = np.percentile(pnl, 5)
    var95 = -q
    cvar95 = -pnl[pnl <= q].mean()
    rng = np.random.default_rng(seed)
    initial = rules()["capital"]["initial"]
    finals = np.full(n_paths, nav, dtype=float)
    if days_left > 0:
        nb = int(np.ceil(days_left / block))
        starts = rng.integers(0, len(pnl) - block, size=(n_paths, nb))
        sims = np.stack([pnl[s:s + block] for s in starts.ravel()]).reshape(n_paths, nb * block)[:, :days_left]
        finals = nav + sims.sum(axis=1)
    ret = finals / initial - 1
    return {"var95": round(float(var95)), "cvar95": round(float(cvar95)), "var95_pct": round(float(var95 / nav * 100), 2),
            "worst_day": round(float(pnl.min())), "days_left": days_left,
            "end": {"p_positive": round(float((ret > 0).mean()), 4), "median": round(float(np.median(ret)) * 100, 2),
                    "p10": round(float(np.percentile(ret, 10)) * 100, 2), "p90": round(float(np.percentile(ret, 90)) * 100, 2)},
            "note": ("以現在的部位套過去 250 個交易日的每日價格變動；到 11/27 的分布假設部位不變（5 日區塊重抽樣 4,000 次）。"
                     f"注意：過去一年加權指數漲 {_tx_1y(ctx):+.0f}%，屬強勢多頭，這個分布可能偏樂觀")}


# ---------------------------------------------------------------- 黑天鵝
def _swan_data() -> dict[str, pd.DataFrame]:
    from src.data import yahoo_gateway as yg

    def fetch():
        d = yg.download(SWAN_TICKERS + ["^TWII"], period="max", interval="1d")
        out = {}
        for k, v in d.items():
            v = v.copy()
            v.index = pd.to_datetime(v.index.date)
            out[k] = v
        return out

    return _cached(f"swan_{now_tw().date()}_{now_tw().hour // 2}", 2 * 3600, fetch)


def _signals(d: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
    def close(t):
        return d[t]["Close"].dropna() if t in d else pd.Series(dtype=float)

    vix, vix3, hyg, jpy, twd = (close(t) for t in SWAN_TICKERS)
    sig = {}
    if len(vix) and len(vix3):
        j = pd.concat([vix, vix3], axis=1, join="inner").dropna()
        sig["VIX 期限結構倒掛（VIX > 3 個月 VIX）"] = j.iloc[:, 0] > j.iloc[:, 1]
    if len(hyg):
        sig["高收益債 HYG 單日 ≤ −1.5%"] = hyg.pct_change() <= -0.015
    if len(jpy):
        sig["日圓急升（美元兌日圓單日 ≤ −2%）"] = jpy.pct_change() <= -0.02
    if len(twd):
        sig["台幣兩日貶逾 1%"] = twd.pct_change(2) >= 0.01
    if len(vix):
        sig["VIX 單日 +25% 以上"] = vix.pct_change() >= 0.25
    return sig


def swan_backtest(d: dict[str, pd.DataFrame], h: int = 20) -> dict[str, dict]:
    """訊號日 t（美股收盤）→ 加權 t 之後第一個交易日開盤買、h 日後收盤；與所有日子比較。"""
    tw = d.get("^TWII")
    if tw is None:
        return {}
    tw = tw.dropna(subset=["Open", "Close"])
    tw = tw[tw.Open > 0]
    o, c = tw.Open.to_numpy(), tw.Close.to_numpy()
    idx = tw.index
    fwd = np.full(len(tw), np.nan)
    dd = np.full(len(tw), np.nan)
    for i in range(len(tw) - h):
        fwd[i] = c[i + h - 1] / o[i] - 1
        dd[i] = tw.Low.to_numpy()[i:i + h].min() / o[i] - 1
    base = pd.DataFrame({"fwd": fwd, "dd": dd}, index=idx).dropna()
    out = {}
    for name, s in _signals(d).items():
        days = s[s].index
        pos = np.searchsorted(idx, days, side="right")  # 美股 t 收盤後的第一個台股交易日
        pos = pos[pos < len(idx)]
        ev = base.reindex(idx[np.unique(pos)]).dropna()
        n = len(ev)
        if n < 5:
            out[name] = {"n": n, "edge": False}
            continue
        diff = ev.fwd.mean() - base.fwd.mean()
        t = diff / (ev.fwd.std(ddof=1) / np.sqrt(n)) if ev.fwd.std(ddof=1) > 0 else 0.0
        out[name] = {"n": int(n), "fwd_mean": round(float(ev.fwd.mean()) * 100, 2), "base_mean": round(float(base.fwd.mean()) * 100, 2),
                     "p_dd5": round(float((ev.dd <= -0.05).mean()), 3), "base_p_dd5": round(float((base.dd <= -0.05).mean()), 3),
                     "t": round(float(t), 2), "edge": bool(n >= 20 and diff <= -0.01 and t <= -2)}
    return out


def black_swan() -> dict:
    d = _swan_data()
    bt = _cached(f"swan_bt_{now_tw().date()}", 86400, lambda: swan_backtest(d))
    sig = _signals(d)
    items = []
    for name, s in sig.items():
        s = s.dropna()
        if s.empty:
            continue
        active = bool(s.iloc[-1])
        b = bt.get(name, {})
        items.append({"name": name, "active": active, "date": str(s.index[-1].date()), "edge": b.get("edge", False),
                      "label": "有預測力" if b.get("edge") else "僅資訊", "backtest": b,
                      "text": (f"歷史 {b['n']} 次：之後 20 日加權平均 {b['fwd_mean']:+.2f}%（平常 {b['base_mean']:+.2f}%），"
                               f"20 日內跌逾 5% 機率 {b['p_dd5'] * 100:.0f}%（平常 {b['base_p_dd5'] * 100:.0f}%）"
                               if b.get("n", 0) >= 5 else "歷史次數不足")})
    try:  # 全市場跌停家數（僅資訊）
        from src.agents import alerts

        lm = alerts.limit_moves(alerts.market_quotes())
        n_down = len(lm.get("down", []))
        items.append({"name": f"全市場跌停 {n_down} 家", "active": n_down >= 30, "edge": False, "label": "僅資訊",
                      "date": now_tw().strftime("%Y-%m-%d"), "text": "30 家以上代表恐慌性賣壓，流動性變差、跌停股可能賣不掉"})
    except Exception as e:  # noqa: BLE001
        log.info("跌停家數失敗：%s", e)
    fi = foreign_futures()
    if fi:
        items.append({"name": f"外資台指期淨未平倉 {fi['net_oi']:+,} 口（{fi['chg']:+,}）", "active": fi["chg"] <= -5000,
                      "edge": False, "label": "僅資訊", "date": fi["date"],
                      "text": "外資單日淨空單增加 5,000 口以上時標示；歷史資料不足以驗證預測力"})
    return {"items": items, "active": [i for i in items if i["active"]]}


def foreign_futures() -> dict | None:
    def fetch():
        import requests

        rows = requests.get("https://openapi.taifex.com.tw/v1/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate",
                            timeout=30).json()
        r = next((x for x in rows if x.get("ContractCode") == "臺股期貨" and "外資" in x.get("Item", "")), None)
        if not r:
            return None
        net = int(float(r.get("OpenInterest(Net)") or r.get("OpenInterest(Net)(Contracts)") or 0))
        prev = _CACHE.get("ffut_prev", (0, None))[1]
        chg = net - prev["net_oi"] if prev and prev["date"] != r["Date"] else 0
        return {"date": r["Date"], "net_oi": net, "chg": chg}

    try:
        v = _cached("ffut", 3600, fetch)
        if v:
            _CACHE["ffut_prev"] = (time.time(), v)
        return v
    except Exception as e:  # noqa: BLE001
        log.info("期交所外資未平倉失敗：%s", e)
        return None


# ---------------------------------------------------------------- 總覽
def snapshot() -> dict:
    def build():
        ctx, pf, prices, nav = _book()
        today = now_tw().date()
        left = trading_days_left(today, comp_end())
        st = stress(ctx, pf, prices, nav)
        mc = margin_call(pf, prices)
        vp = var_and_paths(ctx, pf, nav, left)
        try:
            bs = black_swan()
        except Exception as e:  # noqa: BLE001
            log.warning("黑天鵝指標失敗：%s", e)
            bs = {"items": [], "active": [], "error": str(e)[:120]}
        alerts_ = []
        if mc and mc.get("pct_to_call") is not None and mc["pct_to_call"] < 3:
            alerts_.append({"key": "margin", "level": "嚴重", "title": f"期貨距追繳只剩 {mc['pct_to_call']:.1f}%", "body": mc["text"]})
        elif mc and mc.get("pct_to_call") is not None and mc["pct_to_call"] < 6:
            alerts_.append({"key": "margin-warn", "level": "注意", "title": f"期貨距追繳 {mc['pct_to_call']:.1f}%", "body": mc["text"]})
        s5 = next((s for s in st if s["scenario"] == "加權指數 −5%"), None)
        if s5 and s5["pct_nav"] is not None and s5["pct_nav"] <= -10:
            alerts_.append({"key": "stress5", "level": "注意", "title": f"大盤跌 5% 估計虧 {-s5['total']:,} 元（{s5['pct_nav']:+.1f}%）",
                            "body": f"其中期貨 {s5['futures']:+,} 元、股票 {s5['stock']:+,} 元；跌後報酬率約 {s5['return_after']:+.2f}%"})
        for b in bs["active"]:
            alerts_.append({"key": f"swan-{b['name'][:12]}", "level": "嚴重" if b["edge"] else "注意",
                            "title": f"黑天鵝指標：{b['name']}（{b['label']}）", "body": b["text"]})
        return {"time": now_tw().strftime("%m/%d %H:%M"), "nav": round(nav),
                "return_pct": round((nav / rules()["capital"]["initial"] - 1) * 100, 2), "stress": st, "margin": mc,
                "var": vp, "black_swan": bs, "alerts": alerts_,
                "positions": [{"code": c, "name": stock_meta(c)["name"]} for c in pf.positions],
                "policy": "只提醒、不自動降低曝險（使用者選擇報酬最大化）"}

    return _cached("snapshot", 60, build)
