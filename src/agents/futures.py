"""FuturesAgent：台指期方向建議（衝名次／補回虧損）＋台指賣權避險（只做買方）。

方向模型：
- 特徵（收盤時可取得）：加權指數 1／5／20 日報酬、距月線／季線、RSI14、20 日波動、月線是否在季線上、
  前一晚美股（費半、那斯達克期貨）1／5 日報酬、VIX 水準與變化。
- 目標：未來 h 個交易日（預設 5）加權指數漲或跌。
- 驗證：walk-forward（每 21 天重訓、只用當時以前資料），以「不重疊的 h 日持有」統計樣本外命中率與期望值（扣成本）。
- 門檻：做多、放空各自的樣本外命中率需高於基準且期望值 > 0、交易數足夠，才開放該方向；否則明確回答「沒有優勢、不建議」。

全部規則化計算，不使用 LLM；理由文字只引用計算結果。
"""
from __future__ import annotations

import datetime as dt
import logging
import math

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from src.agents import futures_data as fd
from src.agents import market_data
from src.config import now_tw, rules
from src.state import portfolio as pfm

log = logging.getLogger(__name__)
FEATURES = ["r1", "r5", "r20", "dist_ma20", "dist_ma60", "rsi14", "vol20", "trend", "sox_r1", "sox_r5", "nq_r1",
            "vix", "vix_chg"]
FEATURE_NAMES = {"r1": "加權 1 日報酬", "r5": "加權 5 日報酬", "r20": "加權 20 日報酬", "dist_ma20": "距月線",
                 "dist_ma60": "距季線", "rsi14": "RSI14", "vol20": "20 日波動", "trend": "月線在季線上",
                 "sox_r1": "費半前一晚", "sox_r5": "費半 5 日", "nq_r1": "那斯達克期貨前一晚", "vix": "VIX 水準",
                 "vix_chg": "VIX 變化"}


def cfg() -> dict:
    from src.state import params  # 策略調參 Agent 寫入的覆寫值（config/learned_params.yaml）

    return params.get("futures")


# ---------------------------------------------------------------- 特徵
def _rsi(c: pd.Series, n: int = 14) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


def build_features(tx: pd.DataFrame, us: dict[str, pd.DataFrame], h: int) -> pd.DataFrame:
    c = tx["Close"].astype(float)
    df = pd.DataFrame(index=tx.index)
    df["close"] = c
    df["r1"], df["r5"], df["r20"] = c.pct_change(), c.pct_change(5), c.pct_change(20)
    ma20, ma60 = c.rolling(20).mean(), c.rolling(60).mean()
    df["dist_ma20"], df["dist_ma60"] = c / ma20 - 1, c / ma60 - 1
    df["rsi14"] = _rsi(c)
    df["vol20"] = c.pct_change().rolling(20).std()
    df["trend"] = (ma20 > ma60).astype(float)
    hi, lo = tx["High"].astype(float), tx["Low"].astype(float)
    tr = pd.concat([hi - lo, (hi - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()

    def us_series(t: str, col: str = "Close") -> pd.Series:
        u = us.get(t)
        return u[col].astype(float) if u is not None and not u.empty else pd.Series(dtype=float)

    us_df = pd.DataFrame({"sox": us_series("^SOX"), "nq": us_series("NQ=F"), "vix": us_series("^VIX")}).sort_index()
    us_feat = pd.DataFrame(index=us_df.index)
    us_feat["sox_r1"], us_feat["sox_r5"] = us_df["sox"].pct_change(), us_df["sox"].pct_change(5)
    us_feat["nq_r1"] = us_df["nq"].pct_change()
    us_feat["vix"], us_feat["vix_chg"] = us_df["vix"], us_df["vix"].pct_change()
    # 台股 t 日收盤時，最新的美股收盤是 t 日之前的那一晚（不偷看未來）
    left = df.reset_index().rename(columns={"index": "date", df.index.name or "index": "date"})
    # 兩邊的日期精度可能不同（us / s），merge_asof 要求一致，統一成 ns 且去掉時區
    left["date"] = _naive_ns(left["date"])
    right = us_feat.reset_index().rename(columns={"index": "us_date"})
    right = right.rename(columns={right.columns[0]: "us_date"})
    right["us_date"] = _naive_ns(right["us_date"])
    merged = pd.merge_asof(left.sort_values("date"), right.sort_values("us_date"), left_on="date", right_on="us_date",
                           allow_exact_matches=False, direction="backward")
    merged = merged.set_index("date")
    merged["fwd"] = merged["close"].shift(-h) / merged["close"] - 1
    return merged


def _naive_ns(s: pd.Series) -> pd.Series:
    s = pd.to_datetime(s)
    if getattr(s.dt, "tz", None) is not None:
        s = s.dt.tz_localize(None)
    return s.astype("datetime64[ns]")


def _model():
    return make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=500))


# ---------------------------------------------------------------- 回測
def backtest(feat: pd.DataFrame, h: int, p_long: float, p_short: float, cost: float, min_train: int = 500,
             refit: int = 21) -> dict:
    data = feat.dropna(subset=FEATURES + ["fwd"])
    probs = pd.Series(np.nan, index=data.index)
    for start in range(min_train, len(data), refit):
        train = data.iloc[: max(0, start - h)]  # 訓練資料的目標不能和測試期重疊
        if len(train) < 200:
            continue
        m = _model().fit(train[FEATURES], (train["fwd"] > 0).astype(int))
        test = data.iloc[start: start + refit]
        probs.loc[test.index] = m.predict_proba(test[FEATURES])[:, 1]
    oos = data.loc[probs.dropna().index].copy()
    oos["p"] = probs.dropna()
    sample = oos.iloc[::h]  # 不重疊的 h 日持有
    out = {"h": h, "n_oos_days": int(len(oos)), "base_up_rate": round(float((oos["fwd"] > 0).mean()), 3),
           "period": f"{oos.index[0].date()}～{oos.index[-1].date()}" if len(oos) else None,
           "always_long": {"n": int(len(sample)), "hit": round(float((sample["fwd"] - cost > 0).mean()), 3),
                           "avg_ret_pct": round(float((sample["fwd"] - cost).mean()) * 100, 3)} if len(sample) else None}
    for side, mask, sign in (("long", sample["p"] >= p_long, 1), ("short", sample["p"] <= p_short, -1)):
        t = sample[mask]
        rets = sign * t["fwd"] - cost
        base = out["base_up_rate"] if side == "long" else 1 - out["base_up_rate"]
        out[side] = {"n": int(len(t)), "hit": round(float((rets > 0).mean()), 3) if len(t) else None,
                     "avg_ret_pct": round(float(rets.mean()) * 100, 3) if len(t) else None,
                     "base_rate": round(base, 3)}
    out["all_days_auc_like"] = round(float(((oos["p"] > 0.5) == (oos["fwd"] > 0)).mean()), 3) if len(oos) else None
    return out


def _gate(bt: dict, side: str) -> tuple[bool, str]:
    c = cfg()
    s = bt.get(side) or {}
    name = "做多" if side == "long" else "放空"
    if not s.get("n") or s["n"] < c["min_backtest_trades"]:
        return False, f"{name}：樣本外交易僅 {s.get('n', 0)} 次，少於 {c['min_backtest_trades']} 次，證據不足"
    bench = (bt.get("always_long") or {}).get("avg_ret_pct", 0) if side == "long" else 0.0
    if s["hit"] <= s["base_rate"] or s["avg_ret_pct"] <= max(0.0, bench):
        return False, (f"{name}：樣本外命中率 {s['hit'] * 100:.1f}%（基準 {s['base_rate'] * 100:.1f}%）、"
                       f"平均每次 {s['avg_ret_pct']:+.2f}%" + (f"（一直做多 {bench:+.2f}%）" if side == "long" else "")
                       + "，沒有擇時優勢")
    return True, (f"{name}：樣本外 {s['n']} 次，命中率 {s['hit'] * 100:.1f}%（基準 {s['base_rate'] * 100:.1f}%），"
                  f"平均每次 {s['avg_ret_pct']:+.2f}%（已扣成本）")


_SIG: dict = {}


def signal(target: dt.date | None = None) -> dict:
    """今日方向訊號＋回測門檻（每日快取）。"""
    target = target or now_tw().date()
    if _SIG.get("date") == target:
        return _SIG["v"]
    c = cfg()
    h = c["horizon_days"]
    tx = market_data.taiex(5)
    us = market_data.us_history(5)
    feat = build_features(tx, us, h)
    cost = c["roundtrip_cost_pct"] / 100
    bt = backtest(feat, h, c["p_long"], c["p_short"], cost)
    data = feat.dropna(subset=FEATURES + ["fwd"])
    m = _model().fit(data[FEATURES], (data["fwd"] > 0).astype(int))
    last = feat.dropna(subset=FEATURES).iloc[-1]
    p = float(m.predict_proba(last[FEATURES].to_frame().T)[:, 1][0])
    coefs = m[-1].coef_[0]
    z = (last[FEATURES].astype(float) - m[0].mean_) / m[0].scale_
    contrib = sorted(((float(coefs[i] * z.iloc[i]), FEATURES[i]) for i in range(len(FEATURES))), key=lambda t: -abs(t[0]))
    long_ok, long_why = _gate(bt, "long")
    short_ok, short_why = _gate(bt, "short")
    out = {"date": str(last.name.date()), "horizon_days": h, "p_up": round(p, 3), "close": float(last["close"]),
           "above_ma60": bool(last["dist_ma60"] > 0), "dist_ma60_pct": round(float(last["dist_ma60"]) * 100, 2),
           "atr14": float(last["atr14"]), "backtest": bt, "long_enabled": long_ok, "short_enabled": short_ok,
           "gate_reasons": [long_why, short_why],
           "drivers": [{"feature": FEATURE_NAMES[f], "push": "看多" if v > 0 else "看空", "weight": round(v, 3)}
                       for v, f in contrib[:4]],
           "features": {FEATURE_NAMES[f]: round(float(last[f]), 4) for f in FEATURES}}
    _SIG.update(date=target, v=out)
    return out


# ---------------------------------------------------------------- 部位規劃
def _mode(nav: float, today: dt.date) -> tuple[str, float, float, list[str]]:
    """回傳（模式、每筆風險比例、機率門檻加碼、理由）。"""
    c = cfg()
    r = rules()
    ret = nav / r["capital"]["initial"] - 1
    hist = pfm.load_history()
    peak = max([h["nav"] for h in hist] + [nav, r["capital"]["initial"]])
    dd = nav / peak - 1
    end = dt.date.fromisoformat(str(r["competition"]["end"]))
    from src.agents.compliance import remaining_trading_days

    left = remaining_trading_days(today) if today <= end else 0
    why = [f"目前累積報酬 {ret * 100:+.2f}%、距高點回撤 {dd * 100:+.2f}%、剩 {left} 個交易日"]
    if left <= c["late_days"] and ret > 0:
        return "保名次", c["risk_protect"], 0.03, why + ["比賽末段且已是正報酬（只有正報酬才排名）：降低期貨風險"]
    if ret < 0 or dd <= -c["recovery_drawdown"]:
        extra = "比賽末段仍為負報酬，放寬到最高風險" if left <= c["late_days"] else "提高機率門檻、放寬每筆風險"
        risk = c["risk_recovery_late"] if left <= c["late_days"] else c["risk_recovery"]
        return "補回虧損", risk, 0.02, why + [f"報酬為負或回撤 ≥ {c['recovery_drawdown'] * 100:.0f}%：{extra}"]
    return "衝名次", c["risk_rank"], 0.0, why + ["正常狀態：訊號夠強時以小部位放大報酬"]


def _contract_qty(risk_amt: float, stop_pts: float, margins: dict, max_margin: float) -> tuple[str, int, list[str]]:
    """用微台計算口數；口數夠多時改用小台（1 口小台＝5 口微台）。"""
    mult = {k: v["multiplier"] for k, v in rules()["futures"]["contracts"].items()}
    q_tmf = math.floor(risk_amt / (stop_pts * mult["TMF"]))
    notes = [f"風險金額 {risk_amt:,.0f} 元 ÷（停損 {stop_pts:,.0f} 點 × 微台每點 {mult['TMF']} 元）= {q_tmf} 口微台"]
    root, qty = "TMF", q_tmf
    if q_tmf >= 10:
        root, qty = "MTX", q_tmf // 5
        notes.append(f"微台 {q_tmf} 口 ≥ 10 口，改用小台 {qty} 口（1 口小台＝5 口微台，手續費較省）")
    cap = math.floor(max_margin / margins[root]["initial"]) if margins.get(root) else 0
    if qty > cap:
        notes.append(f"受保證金上限 {max_margin:,.0f} 元限制，下修為 {cap} 口")
        qty = cap
    return root, qty, notes


def _net_existing(pf, orders: list[dict], reasons: list[str], nav: float) -> list[dict]:
    """扣掉已持有的同方向期貨部位（以每點損益金額比較）：已達建議曝險就不再加碼，超出太多時提醒減碼。
    （10/06 已持有大台 2 口、每點 400 元，模型仍建議再做多微台 7 口 → 曝險被重複計算）"""
    held = [d for d in (getattr(pf, "derivs", None) or {}).values() if d.kind == "FUT" and d.qty]
    if not held:
        return orders
    have = sum(d.qty * d.multiplier for d in held)  # 每點損益（元），正＝多、負＝空
    desc = "、".join(f"{d.contract} {'多' if d.qty > 0 else '空'} {abs(d.qty)} 口" for d in held)
    out = []
    for o in orders:
        if o.get("asset") != "FUT":
            out.append(o)
            continue
        mult = rules()["futures"]["contracts"][o["code"]]["multiplier"]
        sign = 1 if o["action"] == "BUY" else -1
        want = o["qty"] * mult  # 建議曝險（每點元，絕對值）
        stop_pts = abs(o["limit_price"] - o["stop"])
        if have * sign <= 0:  # 現有部位方向相反或沒有同方向部位
            reasons.append(f"目前持有 {desc}（每點 {have:+,} 元），與建議方向相反，下單前請先確認是否要平倉")
            out.append(o)
            continue
        remain = want - abs(have)
        if remain < mult:
            ratio = abs(have) / want if want else 0
            msg = f"已持有 {desc}，每點 {abs(have):,} 元，已達建議曝險每點 {want:,} 元，不再加碼"
            if ratio >= 1.5:
                loss = abs(have) * stop_pts
                msg += (f"；現有曝險是建議的 {ratio:.1f} 倍，若跌 {stop_pts:,} 點（停損距離）約虧損 {loss:,.0f} 元"
                        f"（總資產 {loss / nav * 100:.1f}%），可考慮減碼")
            reasons.append(msg)
            continue
        qty = int(remain // mult)
        o = {**o, "qty": qty, "margin": round(o["margin"] / o["qty"] * qty), "max_loss": round(o["max_loss"] / o["qty"] * qty),
             "why_qty": [*o["why_qty"], f"扣除已持有 {desc}（每點 {abs(have):,} 元）後，再加 {qty} 口"]}
        out.append(o)
    return out


def plan(pf, prices: dict, nav: float, alloc: dict | None = None, stock_beta_value: float = 0.0,
         today: dt.date | None = None, us_snapshot: list[dict] | None = None) -> dict:
    """期貨方向建議＋選擇權避險建議。stock_beta_value：股票部位（含今日預計買進）的 β 加權市值。"""
    c = cfg()
    today = today or now_tw().date()
    try:
        sig = signal(today)
    except Exception as e:  # noqa: BLE001
        log.warning("期貨方向模型失敗：%s", e)
        return {"orders": [], "summary": f"期貨方向模型暫時無法計算：{str(e)[:80]}", "signal": None, "hedge": None,
                "reserve": c["min_reserve"], "margin_needed": 0}
    margins = fd.margins()
    mode, risk_pct, p_add, mode_why = _mode(nav, today)
    ym = fd.front_month(today, min_days=c["roll_days"])
    orders, reasons = [], list(mode_why)
    p = sig["p_up"]
    side = None
    if p >= c["p_long"] + p_add and sig["long_enabled"]:
        side = "BUY"
    elif p <= c["p_short"] - p_add and sig["short_enabled"]:
        side = "SELL"
    dir_text = f"方向模型：未來 {sig['horizon_days']} 個交易日上漲機率 {p * 100:.1f}%（模型估計，不是保證）"
    reasons.append(dir_text)
    reasons += sig["gate_reasons"]
    q = fd.quote("MTX", ym)
    price = q.get("price") or sig["close"]
    if side in ("BUY", "SELL"):
        stop_pts = round(c["atr_mult"] * sig["atr14"])
        risk_amt = nav * risk_pct
        max_margin = nav * c["max_margin_pct_nav"]
        root, qty, qty_notes = _contract_qty(risk_amt, stop_pts, margins, max_margin)
        if qty > 0 and side in ("BUY", "SELL"):
            mult = rules()["futures"]["contracts"][root]["multiplier"]
            stop = price - stop_pts if side == "BUY" else price + stop_pts
            take = price + 2 * stop_pts if side == "BUY" else price - 2 * stop_pts
            orders.append({
                "asset": "FUT", "code": root, "contract": f"{root}{ym}", "name": rules()["futures"]["contracts"][root]["name"],
                "action": side, "qty": qty, "limit_price": round(price), "stop": round(stop), "take_profit": round(take),
                "margin": round(qty * margins[root]["initial"]), "max_loss": round(qty * stop_pts * mult),
                "mode": mode, "hold_days": sig["horizon_days"],
                "why": [f"{'做多' if side == 'BUY' else '放空'}理由：{dir_text}",
                        *(g for g in sig["gate_reasons"] if g.startswith("做多" if side == "BUY" else "放空")),
                        "主要依據：" + "、".join(f"{d['feature']}{d['push']}" for d in sig["drivers"][:3])],
                "why_qty": [f"模式「{mode}」：每筆風險 = 總資產 {nav:,.0f} 元 × {risk_pct * 100:.1f}%", *qty_notes,
                            f"停損距離 = {c['atr_mult']} × 加權指數 ATR14 {sig['atr14']:,.0f} 點 = {stop_pts:,.0f} 點"],
                "exit_plan": [f"停損：{'跌破' if side == 'BUY' else '漲破'} {round(stop):,} 點全數平倉",
                              f"停利：{'漲到' if side == 'BUY' else '跌到'} {round(take):,} 點（2 倍停損距離）平倉",
                              f"時間出場：持有滿 {sig['horizon_days']} 個交易日仍未觸及就平倉；結算日 "
                              f"{fd.settlement_date(ym)} 前 {c['roll_days']} 天轉倉"],
            })
        else:
            reasons.append("訊號成立，但依風險與保證金上限算出 0 口，不建議操作")
    elif c["overlay"]["enabled"] and mode in c["overlay"].get("modes", ["補回虧損"]):
        side = "OVERLAY"
    else:
        reasons.append("目前沒有足夠的擇時優勢，不建議做多或放空期貨")
    if side == "OVERLAY":
        reasons = [r for r in reasons if not r.startswith("目前沒有足夠的擇時優勢")]
        al = sig["backtest"].get("always_long") or {}
        if not sig["above_ma60"]:
            reasons.append(f"{mode}模式可考慮加碼大盤曝險，但加權指數在季線下（{sig['dist_ma60_pct']:+.2f}%），風控規定不加碼")
        elif not al or al.get("avg_ret_pct", 0) <= 0:
            reasons.append(f"{mode}模式：近年一直做多的平均報酬不為正，不加碼大盤曝險")
        else:
            stop_pts = round(c["atr_mult"] * sig["atr14"])
            root, qty, qty_notes = _contract_qty(nav * c["overlay"]["risk"], stop_pts, margins, nav * c["max_margin_pct_nav"])
            if qty > 0:
                mult = rules()["futures"]["contracts"][root]["multiplier"]
                orders.append({
                    "asset": "FUT", "code": root, "contract": f"{root}{ym}", "name": rules()["futures"]["contracts"][root]["name"],
                    "action": "BUY", "qty": qty, "limit_price": round(price), "stop": round(price - stop_pts),
                    "take_profit": round(price + 2 * stop_pts), "margin": round(qty * margins[root]["initial"]),
                    "max_loss": round(qty * stop_pts * mult), "mode": f"{mode}：加碼大盤曝險", "hold_days": sig["horizon_days"],
                    "why": ["這筆不是預測優勢，而是放大對大盤的曝險，" + ("用來加快補回虧損" if mode == "補回虧損" else
                                                                  "用期貨戶閒置資金提高總資產報酬（衝名次）"),
                            f"依據：{sig['backtest']['period']} 樣本外每 {sig['horizon_days']} 日一直做多 {al['n']} 次，"
                            f"勝率 {al['hit'] * 100:.1f}%、平均 {al['avg_ret_pct']:+.2f}%（已扣成本）；這段期間大盤偏多，空頭時會同樣放大虧損",
                            f"風控條件：加權指數在季線上（{sig['dist_ma60_pct']:+.2f}%）才加碼"],
                    "why_qty": [f"{mode}加碼：每筆風險 = 總資產 {nav:,.0f} 元 × {c['overlay']['risk'] * 100:.1f}%", *qty_notes,
                                f"停損距離 = {c['atr_mult']} × ATR14 {sig['atr14']:,.0f} 點 = {stop_pts:,.0f} 點"],
                    "exit_plan": [f"停損：跌破 {round(price - stop_pts):,} 點全數平倉",
                                  f"停利：漲到 {round(price + 2 * stop_pts):,} 點平倉",
                                  ("跌破季線或累積報酬轉正時平倉" if mode == "補回虧損" else "跌破季線時平倉")
                                  + f"；結算日 {fd.settlement_date(ym)} 前 {c['roll_days']} 天轉倉"],
                })

    orders = _net_existing(pf, orders, reasons, nav)
    hedge = hedge_plan(sig, nav, stock_beta_value, alloc, us_snapshot, today)
    margin_needed = sum(o["margin"] for o in orders)
    reserve = max(c["min_reserve"], round(margin_needed * c["margin_buffer"]) + (hedge["premium"] if hedge else 0))
    conditions = watch_conditions(sig, nav, alloc, us_snapshot, stock_beta_value, mode)
    reference = sample_sizing(sig, nav, margins, ym, price, today)
    return {"orders": orders, "hedge": hedge, "signal": sig, "mode": mode, "reasons": reasons, "contract_month": ym,
            "quote": q, "margins": margins, "margin_needed": margin_needed, "reserve": reserve,
            "conditions": conditions, "reference": reference,
            "summary": (f"{'做多' if orders[0]['action'] == 'BUY' else '放空'} {orders[0]['name']} {orders[0]['qty']} 口"
                        if orders else "期貨不操作") + ("；買進賣權避險" if hedge else "")}


def watch_conditions(sig: dict, nav: float, alloc: dict | None, us_snapshot: list[dict] | None, beta_value: float,
                     mode: str) -> list[dict]:
    """每一種期貨操作的觸發條件、目前數值、是否達成（沒有建議時，讓使用者知道還差什麼）。"""
    c = cfg()
    h = c["hedge"]
    r = rules()
    p = sig["p_up"]
    bt = sig["backtest"]
    ret = nav / r["capital"]["initial"] - 1
    hist = pfm.load_history()
    peak = max([x["nav"] for x in hist] + [nav, r["capital"]["initial"]])
    dd = nav / peak - 1
    tx = (alloc or {}).get("taiex") or {}
    vix = next((u for u in (us_snapshot or []) if u.get("ticker") == "^VIX"), None)
    al = bt.get("always_long") or {}
    lg = bt.get("long") or {}
    out = [
        {"name": "做多台指期（擇時）", "met": bool(p >= c["p_long"] and sig["long_enabled"]),
         "need": f"上漲機率 ≥ {c['p_long'] * 100:.0f}%，且回測證明模型做多贏過「一直做多」",
         "now": f"上漲機率 {p * 100:.1f}%（{'已達' if p >= c['p_long'] else '未達'}）；回測模型做多平均 "
                f"{lg.get('avg_ret_pct', '—')}% vs 一直做多 {al.get('avg_ret_pct', '—')}%（{'有' if sig['long_enabled'] else '沒有'}優勢）"},
        {"name": "放空台指期（擇時）", "met": bool(p <= c["p_short"] and sig["short_enabled"]),
         "need": f"上漲機率 ≤ {c['p_short'] * 100:.0f}%，且回測放空有正報酬、樣本 ≥ {c['min_backtest_trades']} 次",
         "now": f"上漲機率 {p * 100:.1f}%；回測放空樣本 {(bt.get('short') or {}).get('n', 0)} 次"},
        {"name": "補回虧損：加碼大盤曝險", "met": bool((ret < 0 or dd <= -c["recovery_drawdown"]) and sig["above_ma60"]),
         "need": f"累積報酬轉負或回撤 ≥ {c['recovery_drawdown'] * 100:.0f}%，且加權在季線上",
         "now": f"累積報酬 {ret * 100:+.2f}%、回撤 {dd * 100:+.2f}%、加權距季線 {sig['dist_ma60_pct']:+.2f}%"},
        {"name": "買賣權避險（只做買方）",
         "met": bool(beta_value > 0 and (tx.get("rsi14", 0) >= h["taiex_rsi"] or tx.get("dist_ma60_pct", 0) >= h["dist_ma60_pct"]
                                         or (vix and vix.get("change_pct", 0) >= h["vix_jump_pct"]) or 1 - p >= h["p_down"])),
         "need": f"任一成立：加權 RSI ≥ {h['taiex_rsi']}、距季線 ≥ {h['dist_ma60_pct']}%、VIX 單日 ≥ +{h['vix_jump_pct']}%、"
                 f"下跌機率 ≥ {h['p_down'] * 100:.0f}%",
         "now": f"RSI {tx.get('rsi14', '—')}、距季線 {tx.get('dist_ma60_pct', '—')}%、"
                f"VIX {vix['change_pct']:+.1f}%" if vix else f"RSI {tx.get('rsi14', '—')}、距季線 {tx.get('dist_ma60_pct', '—')}%、VIX 無資料"},
    ]
    out[-1]["now"] += f"、下跌機率 {(1 - p) * 100:.1f}%"
    return out


def sample_sizing(sig: dict, nav: float, margins: dict, ym: str, price: float, today: dt.date) -> dict:
    """參考試算（不是建議）：如果自行判斷要做 1 口，要多少保證金、碰到停損會虧多少。"""
    c = cfg()
    stop_pts = round(c["atr_mult"] * sig["atr14"])
    rows = []
    for root in ("TMF", "MTX"):
        mult = rules()["futures"]["contracts"][root]["multiplier"]
        mg = (margins.get(root) or {}).get("initial")
        loss = stop_pts * mult
        rows.append({"contract": f"{root}{ym}", "name": rules()["futures"]["contracts"][root]["name"], "price": round(price),
                     "margin": mg, "stop_points": stop_pts, "loss_at_stop": loss,
                     "loss_pct_nav": round(loss / nav * 100, 2) if nav else None,
                     "notional": round(price * mult)})
    put = None
    try:
        hy = fd.front_month(today, min_days=c["hedge"]["min_days_to_expiry"])
        target = sig["close"] * (1 - c["hedge"]["otm_pct"])
        chain = [r for r in fd.put_chain(hy) if r["price"] and r.get("bid") and r.get("ask") and r["strike"] <= target
                 and (r["ask"] - r["bid"]) / r["ask"] <= 0.15]
        if chain:
            best = max(chain, key=lambda r: r["strike"])
            put = {"contract": best["contract"], "strike": best["strike"], "price": best["price"],
                   "premium": round(best["price"] * 50), "protect_from": best["strike"]}
    except Exception:  # noqa: BLE001
        log.debug("TXO put hedge 試算失敗", exc_info=True)
    return {"note": "僅供參考，不是建議：系統目前判斷沒有優勢，若自行操作請先確認風險",
            "stop_rule": f"停損以加權 ATR14 的 {c['atr_mult']} 倍 ≈ {stop_pts:,} 點估算", "futures": rows, "put": put}


def hedge_plan(sig: dict, nav: float, beta_value: float, alloc: dict | None, us_snapshot: list[dict] | None,
               today: dt.date) -> dict | None:
    """台指賣權避險（只做買方）：觸發條件成立且有股票部位時，買價外 3～5% 的賣權。"""
    c = cfg()["hedge"]
    triggers = []
    tx = (alloc or {}).get("taiex") or {}
    if tx.get("rsi14", 0) >= c["taiex_rsi"]:
        triggers.append(f"加權 RSI14 {tx['rsi14']} ≥ {c['taiex_rsi']}（過熱）")
    if tx.get("dist_ma60_pct", 0) >= c["dist_ma60_pct"]:
        triggers.append(f"加權距季線 {tx['dist_ma60_pct']:+.1f}% ≥ {c['dist_ma60_pct']}%（漲多）")
    vix = next((u for u in (us_snapshot or []) if u.get("ticker") == "^VIX"), None)
    if vix and vix.get("change_pct", 0) >= c["vix_jump_pct"]:
        triggers.append(f"VIX 單日 {vix['change_pct']:+.1f}% ≥ {c['vix_jump_pct']}%（恐慌升溫）")
    if sig and sig["p_up"] <= 1 - c["p_down"]:
        triggers.append(f"方向模型下跌機率 {(1 - sig['p_up']) * 100:.1f}% ≥ {c['p_down'] * 100:.0f}%")
    if not triggers or beta_value <= 0:
        return None
    ym = fd.front_month(today, min_days=c["min_days_to_expiry"])
    # 比賽禁止對流動性差的標的異常交易：只選有買賣報價、價差 ≤ 15% 的履約價
    chain = [r for r in fd.put_chain(ym) if r["price"] and r.get("bid") and r.get("ask")
             and (r["ask"] - r["bid"]) / r["ask"] <= 0.15]
    spot = sig["close"]
    target = spot * (1 - c["otm_pct"])
    cands = [r for r in chain if r["strike"] <= target]
    if not cands:
        return None
    put = max(cands, key=lambda r: r["strike"])
    hedge_value = beta_value * c["hedge_ratio"]
    qty = max(1, round(hedge_value / (spot * 50)))
    budget = nav * c["premium_budget_pct"]
    per = put["price"] * 50
    if qty * per > budget:
        qty = math.floor(budget / per)
    if qty <= 0:
        return None
    premium = round(qty * per)
    return {"asset": "OPT", "code": "TXO", "contract": put["contract"], "name": f"台指賣權 {ym[4:]}月 {int(put['strike'])}",
            "action": "BUY", "qty": qty, "limit_price": put["price"], "strike": put["strike"], "premium": premium,
            "max_loss": premium, "triggers": triggers,
            "why": [*triggers,
                    f"股票部位 β 加權市值 {beta_value:,.0f} 元 × 避險比例 {c['hedge_ratio'] * 100:.0f}% ≈ "
                    f"{qty} 口（每口約當 {spot * 50:,.0f} 元）",
                    f"權利金 {put['price']} 點 × 50 元 × {qty} 口 = {premium:,} 元（上限：總資產 {c['premium_budget_pct'] * 100:.1f}%）",
                    f"加權指數跌破 {int(put['strike']):,} 點後開始有保護；最大損失就是權利金 {premium:,} 元",
                    f"合約 {ym} 月，結算日 {fd.settlement_date(ym)}"],
            "exit_plan": ["觸發條件解除且權利金仍有價值時賣出回收", f"結算前 {c['min_days_to_expiry']} 天內不再持有，改評估次月"]}
