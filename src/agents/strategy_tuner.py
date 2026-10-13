"""策略調參 Agent：以「實戰勝算報酬最大化」為目標，用十年樣本外回測自動調整各演算法參數。

目標：每個演算法都朝「樣本外年化報酬最大」調整，主策略（綜合排名＋結單轉股，5 檔起）以年化中位 90% 為達標線。
防過度擬合（實戰比回測重要）：
1. 預測一律用 walk-forward 樣本外結果（data/strategy/wf_preds.csv.gz：每期只用當時以前的資料訓練）
2. 參數搜尋只看「前半段」期間（訓練），「後半段」期間（驗證）不參與挑選；
   新參數要在訓練段贏現行 min_gain 以上、驗證段不能輸、各起始日最差情況不能明顯變差，才寫入
3. 每次修改寫入 data/tuning_log.json（含修改前參數，可用 `python -m src.main tune --rollback` 回復）
4. 不改競賽規則：持股檔數下限 5（rules：最少持有 5 檔）、成本固定為競賽來回 0.585%

涵蓋演算法：
- 投資組合（綜合排名＋結單轉股）：max_positions、min_hold_days、swap_margin、profit_swap_min_pnl、profit_swap_margin、expiry_days
- 當沖：stop_atr、reward_risk、min_stop_pct（日 K 近似回測，取「先停損／先停利」平均，扣真實成本）
- 期貨：p_long、p_short（樣本外邏輯迴歸，需同時贏「一直做多」才算有優勢）
回測結果寫入 data/strategy/scorecard.json（模型頁「演算法回測總表」）。
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import time

import numpy as np
import pandas as pd

from src.config import DATA_DIR, settings
from src.state import params

log = logging.getLogger(__name__)
DIR = DATA_DIR / "strategy"
PRED_FILE = DIR / "wf_preds.csv.gz"
SCORECARD = DIR / "scorecard.json"
COST = 0.00585          # 競賽來回成本：證交稅 0.3% ＋ 手續費 0.1425% × 2
TOP_N = 150             # 每天只存綜合排名前 150 名（其餘視為該日最低預期報酬）
TARGET_CAGR = 0.90      # 主策略達標線
OFFSETS = (0, 10, 20, 30, 40)   # 起始日錯開 0～40 個交易日，看穩健度（不是只靠某一天進場）
MIN_GAIN = 0.03         # 訓練段年化中位需多 3 個百分點
MAX_WORST_DROP = 0.05   # 全期最差起始日年化不能少超過 5 個百分點
MIN_POSITIONS = 5       # 競賽規定最少持有 5 檔

PF_KEYS = ("max_positions", "min_hold_days", "swap_margin", "profit_swap_min_pnl", "profit_swap_margin", "expiry_days")
PF_GRID = {
    "max_positions": [5, 6, 8, 10],
    "min_hold_days": [0, 5, 10, 15, 20],
    "swap_margin": [0.0, 0.01, 0.02, 0.03, 0.05],
    "profit_swap_min_pnl": [0.05, 0.10, 0.15, 0.20, 9.0],   # 9.0＝不啟用獲利結單
    "profit_swap_margin": [0.0, 0.01, 0.02, 0.03],
    "expiry_days": [20, 40, 60, 999],                      # 999＝不設到期
}
DT_GRID = {"stop_atr": [0.3, 0.5, 0.8, 1.0, 1.5], "reward_risk": [1.0, 1.5, 2.0, 3.0], "min_stop_pct": [0.5, 0.8, 1.2]}
FUT_GRID = {"p_long": [0.55, 0.60, 0.65, 0.70], "p_short": [0.30, 0.35, 0.40, 0.45]}


# ---------------------------------------------------------------- 樣本外預測
def load_preds() -> pd.DataFrame:
    if not PRED_FILE.exists():
        return pd.DataFrame(columns=["date", "code", "e", "p", "e_floor"])
    df = pd.read_csv(PRED_FILE, dtype={"code": str})
    df["date"] = pd.to_datetime(df["date"])
    return df


def _compact(test: pd.DataFrame) -> pd.DataFrame:
    t = test[["date", "code", "e", "p"]].copy()
    g = t.groupby("date")
    t["s"] = g["e"].rank(pct=True) + g["p"].rank(pct=True)
    t["e_floor"] = g["e"].transform("min")
    t = t.sort_values(["date", "s"], ascending=[True, False]).groupby("date").head(TOP_N)
    return t[["date", "code", "e", "p", "e_floor"]]


def extend_preds(data: pd.DataFrame, min_new: int = 20, budget_sec: float = 900) -> int:
    """新的交易日累積 ≥ min_new 天才補一期樣本外預測：只用「新日期前一個 horizon」以前有標籤的資料訓練。
    data＝alpha_model.build_panel 的結果。回傳新增天數。"""
    from src.agents import alpha_model as am

    t0 = time.time()
    old = load_preds()
    last = old["date"].max() if len(old) else pd.Timestamp("1900-01-01")
    all_dates = np.sort(data["date"].unique())
    new_dates = all_dates[all_dates > np.datetime64(last)]
    if len(new_dates) < min_new:
        return 0
    cfg = params.alpha()
    hp = {**am.DEFAULT_HP, **cfg.get("hyperparams", {})}
    start_i = int(np.searchsorted(all_dates, new_dates[0]))
    embargo = all_dates[max(0, start_i - cfg["horizon"] - 1)]
    lab = data.dropna(subset=["y_newhigh", "y_ret"])
    ld = np.sort(lab["date"].unique())
    train = lab[lab["date"].isin(ld[::cfg["sample_every"]]) & (lab["date"] < embargo)]
    if len(train) < 5000:
        return 0
    clf, reg = am._fit(train, hp)
    test = data[data["date"].isin(new_dates)].copy()
    test["p"] = clf.predict_proba(test[am.FEATURES])[:, 1]
    test["e"] = reg.predict(test[am.FEATURES])
    out = pd.concat([old, _compact(test)], ignore_index=True)
    DIR.mkdir(parents=True, exist_ok=True)
    w = out.copy()
    w["date"] = pd.to_datetime(w["date"]).dt.strftime("%Y-%m-%d")
    w.round(5).to_csv(PRED_FILE, index=False, compression="gzip")
    log.info("策略調參：新增 %d 天樣本外預測（%.0f 秒）", len(new_dates), time.time() - t0)
    if time.time() - t0 > budget_sec:
        log.warning("補預測耗時 %.0f 秒，超過預算 %.0f 秒", time.time() - t0, budget_sec)
    return int(len(new_dates))


# ---------------------------------------------------------------- 投資組合模擬
class Sim:
    """把預測與收盤價整理成陣列，模擬時不再碰 pandas。"""

    def __init__(self, preds: pd.DataFrame, panel: dict[str, pd.DataFrame]):
        dates = sorted(preds["date"].unique())
        codes = sorted(c for c in preds["code"].unique() if c in panel)
        cl = pd.DataFrame({c: panel[c]["Close"] for c in codes})
        cl.index = pd.to_datetime(cl.index)
        cl = cl.reindex(pd.DatetimeIndex(dates))
        self.dates = list(pd.DatetimeIndex(dates))
        self.close = cl.to_numpy(dtype=float)
        idx = {c: i for i, c in enumerate(codes)}
        self.top: list[list[int]] = []
        self.e: list[dict[int, float]] = []
        self.floor: list[float] = []
        for _, g in preds[preds["code"].isin(idx)].groupby("date", sort=True):
            ci = [idx[c] for c in g["code"]]
            self.top.append(ci)
            self.e.append(dict(zip(ci, g["e"].astype(float))))
            self.floor.append(float(g["e_floor"].iloc[0]))
        self.n = len(self.dates)

    def run(self, p: dict, i0: int, i1: int) -> np.ndarray:
        """每天收盤檢查一次：符合結單轉股條件就全部賣出、同日換入綜合排名最佳未持有股；空位依排名補滿（等權）。"""
        N = int(p["max_positions"])
        mh, sm = p["min_hold_days"], p["swap_margin"]
        pp, pm, ex = p["profit_swap_min_pnl"], p["profit_swap_margin"], p["expiry_days"]
        hold: dict[int, list] = {}   # code → [units, entry_i, entry_px, last]
        cash, navs = 1.0, []
        for t in range(i0, i1):
            px = self.close[t]
            for c, h in hold.items():
                if not np.isnan(px[c]):
                    h[3] = px[c]
            navs.append(cash + sum(h[0] * h[3] for h in hold.values()))
            e, fl = self.e[t], self.floor[t]
            avail = [c for c in self.top[t] if not np.isnan(px[c])]
            for c in list(hold):
                h = hold[c]
                best = next((x for x in avail if x not in hold), None)
                if best is None or np.isnan(px[c]):
                    continue
                hd, pnl = t - h[1], h[3] / h[2] - 1
                gain = e[best] - e.get(c, fl) - COST
                if (hd >= ex and gain > 0) or (pnl >= pp and gain > pm) or (hd >= mh and gain > sm):
                    val = h[0] * h[3] * (1 - COST)
                    del hold[c]
                    hold[best] = [val / px[best], t, px[best], px[best]]
            if len(hold) < N and cash > 1e-9:
                free = [x for x in avail if x not in hold][:N - len(hold)]
                each = cash / len(free) if free else 0.0
                for x in free:
                    hold[x] = [each * (1 - COST / 2) / px[x], t, px[x], px[x]]
                    cash -= each
        return np.array(navs)


def _stats(nav: np.ndarray, d0: pd.Timestamp, d1: pd.Timestamp) -> dict:
    yrs = max((d1 - d0).days / 365.25, 0.1)
    return {"cagr": float((nav[-1] / nav[0]) ** (1 / yrs) - 1), "max_dd": float((nav / np.maximum.accumulate(nav) - 1).min())}


def evaluate_pf(sim: Sim, p: dict, i0: int, i1: int) -> dict:
    rs = []
    for off in OFFSETS:
        s = i0 + off
        if i1 - s < 60:
            continue
        rs.append(_stats(sim.run(p, s, i1), sim.dates[s], sim.dates[i1 - 1]))
    cg = [r["cagr"] for r in rs]
    return {"median": float(np.median(cg)), "worst": float(min(cg)), "max_dd": float(np.median([r["max_dd"] for r in rs])),
            "from": str(sim.dates[i0].date()), "to": str(sim.dates[i1 - 1].date())}


def current_pf() -> dict:
    st = params.strategy()
    return {k: st[k] for k in PF_KEYS}


def _search(score, cur: dict, grid: dict, rounds: int = 2) -> tuple[dict, float]:
    """座標下降：一次只動一個參數，重複 rounds 輪（只用訓練段分數）。"""
    best, best_s = dict(cur), score(cur)
    for _ in range(rounds):
        moved = False
        for k, vals in grid.items():
            for v in vals:
                if v == best[k]:
                    continue
                cand = {**best, k: v}
                s = score(cand)
                if s > best_s + 1e-9:
                    best, best_s, moved = cand, s, True
        if not moved:
            break
    return best, best_s


def tune_portfolio(sim: Sim) -> tuple[dict | None, dict]:
    cur = current_pf()
    half = sim.n // 2
    cache: dict[tuple, dict] = {}

    def ev(p, i0, i1):
        key = (tuple(p[k] for k in PF_KEYS), i0, i1)
        if key not in cache:
            cache[key] = evaluate_pf(sim, p, i0, i1)
        return cache[key]

    grid = {k: [v for v in vals if k != "max_positions" or v >= MIN_POSITIONS] for k, vals in PF_GRID.items()}
    best, _ = _search(lambda p: ev(p, 0, half)["median"], cur, grid)
    res = {"current": cur, "candidate": best,
           "cur_train": ev(cur, 0, half), "new_train": ev(best, 0, half),
           "cur_valid": ev(cur, half, sim.n), "new_valid": ev(best, half, sim.n),
           "cur_full": ev(cur, 0, sim.n), "new_full": ev(best, 0, sim.n)}
    ok = (best != cur and res["new_train"]["median"] - res["cur_train"]["median"] >= MIN_GAIN
          and res["new_valid"]["median"] >= res["cur_valid"]["median"]
          and res["new_full"]["worst"] >= res["cur_full"]["worst"] - MAX_WORST_DROP)
    res["accepted"] = bool(ok)
    if not ok:
        return None, res
    pct = lambda x: f"{x * 100:.1f}%"  # noqa: E731
    return ({"param": "strategy.portfolio", "before": cur, "after": best,
             "metric_before": round(res["cur_full"]["median"], 4), "metric_after": round(res["new_full"]["median"], 4),
             "reason": (f"策略調參 Agent：十年樣本外投資組合模擬，訓練段（{res['cur_train']['from']}～{res['cur_train']['to']}）"
                        f"年化中位 {pct(res['cur_train']['median'])} → {pct(res['new_train']['median'])}，"
                        f"驗證段（{res['cur_valid']['from']}～{res['cur_valid']['to']}，未參與挑選）"
                        f"{pct(res['cur_valid']['median'])} → {pct(res['new_valid']['median'])}，"
                        f"全期 {pct(res['cur_full']['median'])} → {pct(res['new_full']['median'])}"
                        f"（最差起始日 {pct(res['cur_full']['worst'])} → {pct(res['new_full']['worst'])}）")}, res)


# ---------------------------------------------------------------- 當沖
def _dt_eval(f: dict, c: dict, days: pd.DatetimeIndex) -> float | None:
    """做多訊號每筆平均淨報酬（%，先停損／先停利平均），只算 days 期間。"""
    from src.agents import daytrade

    old = daytrade.cfg
    daytrade.cfg = lambda: c  # 暫時套用候選參數
    try:
        w, b, _ = daytrade._signal_returns(f, "long")
    finally:
        daytrade.cfg = old
    rank = f["score"].shift(1).loc[days].rank(axis=1, ascending=False)
    pool = rank <= c["prefilter_n"]
    v = ((w + b) / 2).loc[days].where(pool).stack().dropna()
    return float(v.mean()) if len(v) >= 50 else None


def tune_daytrade(ctx: dict, target: dt.date) -> tuple[dict | None, dict]:
    from src.agents import daytrade

    base = daytrade.cfg()
    codes = list(daytrade.eligible() or {}) or ctx["codes"]
    codes = [c for c in codes if c in ctx["panel"]]
    f = daytrade._features(ctx["panel"], codes, target, base["backtest_days"] + base["history_days"] + 30)
    days = f["score"].index[-base["backtest_days"]:]
    if len(days) < 120:
        return None, {"error": "資料不足"}
    tr, va = days[: len(days) // 2], days[len(days) // 2:]
    keys = tuple(DT_GRID)
    cur = {k: base[k] for k in keys}
    cache: dict[tuple, float | None] = {}

    def ev(p, d):
        key = (tuple(p[k] for k in keys), d[0], d[-1])
        if key not in cache:
            cache[key] = _dt_eval(f, {**base, **p}, d)
        return cache[key]

    best, _ = _search(lambda p: ev(p, tr) if ev(p, tr) is not None else -99, cur, DT_GRID)
    res = {"current": cur, "candidate": best, "cur_train": ev(cur, tr), "new_train": ev(best, tr),
           "cur_valid": ev(cur, va), "new_valid": ev(best, va), "cur_full": ev(cur, days), "new_full": ev(best, days),
           "unit": "每筆平均淨報酬 %"}
    ok = (best != cur and None not in (res["new_train"], res["cur_train"], res["new_valid"], res["cur_valid"])
          and res["new_train"] - res["cur_train"] >= 0.05 and res["new_valid"] > res["cur_valid"])
    res["accepted"] = bool(ok)
    if not ok:
        return None, res
    return ({"param": "daytrade", "before": cur, "after": best, "metric_before": round(res["cur_full"], 3),
             "metric_after": round(res["new_full"], 3),
             "reason": (f"策略調參 Agent：當沖做多每筆平均淨報酬 訓練段 {res['cur_train']:+.2f}% → {res['new_train']:+.2f}%，"
                        f"驗證段（未參與挑選）{res['cur_valid']:+.2f}% → {res['new_valid']:+.2f}%")}, res)


# ---------------------------------------------------------------- 期貨
def tune_futures() -> tuple[dict | None, dict]:
    """期貨方向門檻：樣本外交易平均報酬需同時贏「一直做多」，前後兩半都成立才採用。"""
    from src.agents import futures

    c = futures.cfg()
    h = c["horizon_days"]
    from src.agents import market_data

    feat = futures.build_features(market_data.taiex(5), market_data.us_history(5), h)
    cost = c["roundtrip_cost_pct"] / 100
    data = feat.dropna(subset=futures.FEATURES + ["fwd"])
    probs = pd.Series(np.nan, index=data.index)
    for start in range(500, len(data), 21):   # 與 futures.backtest 相同的滾動訓練，只算一次機率
        train = data.iloc[: max(0, start - h)]
        if len(train) < 200:
            continue
        m = futures._model().fit(train[futures.FEATURES], (train["fwd"] > 0).astype(int))
        test = data.iloc[start: start + 21]
        probs.loc[test.index] = m.predict_proba(test[futures.FEATURES])[:, 1]
    oos = data.loc[probs.dropna().index].assign(p=probs.dropna()).iloc[::h]
    if len(oos) < 40:
        return None, {"error": "資料不足"}
    halves = (oos.iloc[: len(oos) // 2], oos.iloc[len(oos) // 2:])

    def edge(p: dict, s: pd.DataFrame) -> float:
        """擇時策略（多／空／空手）每期平均報酬 − 一直做多每期平均報酬。"""
        pos = np.where(s["p"] >= p["p_long"], 1, np.where(s["p"] <= p["p_short"], -1, 0))
        r = pos * s["fwd"] - np.abs(pos) * cost
        return float(r.mean() - (s["fwd"] - cost).mean())

    cur = {"p_long": c["p_long"], "p_short": c["p_short"]}
    best, _ = _search(lambda p: edge(p, halves[0]), cur, FUT_GRID)
    res = {"current": cur, "candidate": best, "cur_train": edge(cur, halves[0]), "new_train": edge(best, halves[0]),
           "cur_valid": edge(cur, halves[1]), "new_valid": edge(best, halves[1]), "cur_full": edge(cur, oos),
           "new_full": edge(best, oos), "unit": "每期（5 日）平均報酬減一直做多"}
    ok = best != cur and res["new_train"] > res["cur_train"] and res["new_valid"] > max(res["cur_valid"], 0.0)
    res["accepted"] = bool(ok)
    if not ok:
        return None, res
    return ({"param": "futures", "before": cur, "after": best, "metric_before": round(res["cur_full"], 5),
             "metric_after": round(res["new_full"], 5),
             "reason": (f"策略調參 Agent：期貨擇時超越一直做多 訓練段 {res['cur_train'] * 100:+.2f}% → {res['new_train'] * 100:+.2f}%，"
                        f"驗證段 {res['cur_valid'] * 100:+.2f}% → {res['new_valid'] * 100:+.2f}%（每 5 日）")}, res)


# ---------------------------------------------------------------- 主流程
def run(ctx: dict, target: dt.date, data_builder=None, extend: bool = True) -> tuple[list[dict], dict]:
    """回傳 (changes, scorecard)。changes 交給 tuner.apply 寫入；scorecard 存檔給前端。"""
    t0 = time.time()
    changes, card = [], {"generated": dt.datetime.now().isoformat(timespec="seconds"), "target_cagr": TARGET_CAGR,
                         "algorithms": []}
    if extend and data_builder is not None:
        try:
            card["new_pred_days"] = extend_preds(data_builder())
        except Exception as e:  # noqa: BLE001
            log.warning("補樣本外預測失敗：%s", e)
    preds = load_preds()
    if len(preds):
        sim = Sim(preds, ctx["panel"])
        ch, res = tune_portfolio(sim)
        if ch:
            changes.append(ch)
        final = res["new_full"] if ch else res["cur_full"]
        card["algorithms"].append({
            "name": "投資組合（綜合排名＋結單轉股）", "metric": "年化報酬（5 種起始日中位數）", "value": round(final["median"], 4),
            "worst": round(final["worst"], 4), "max_dd": round(final["max_dd"], 4), "target": TARGET_CAGR,
            "meets_target": final["median"] >= TARGET_CAGR, "period": f"{final['from']}～{final['to']}",
            "params": ch["after"] if ch else res["current"], "detail": _num(res), "changed": bool(ch)})
    for name, fn, metric in (("當沖（做多訊號）", lambda: tune_daytrade(ctx, target), "每筆平均淨報酬 %"),
                             ("期貨方向擇時", tune_futures, "每 5 日超越一直做多")):
        try:
            ch, res = fn()
        except Exception as e:  # noqa: BLE001
            log.warning("%s 調參失敗：%s", name, e)
            card["algorithms"].append({"name": name, "error": str(e)})
            continue
        if ch:
            changes.append(ch)
        v = res.get("new_full") if ch else res.get("cur_full")
        card["algorithms"].append({"name": name, "metric": metric, "value": None if v is None else round(v, 4),
                                   "has_edge": v is not None and v > 0, "params": ch["after"] if ch else res.get("current"),
                                   "detail": _num(res), "changed": bool(ch),
                                   "note": "沒有正報酬：不是主力策略，系統只當參考" if v is None or v <= 0 else ""})
    card["seconds"] = round(time.time() - t0)
    DIR.mkdir(parents=True, exist_ok=True)
    SCORECARD.write_text(json.dumps(card, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return changes, card


def _num(x):
    if isinstance(x, dict):
        return {k: _num(v) for k, v in x.items()}
    if isinstance(x, float):
        return round(x, 4)
    return x


def scorecard() -> dict:
    return json.loads(SCORECARD.read_text(encoding="utf-8")) if SCORECARD.exists() else {}


def apply_change(cur: dict, ch: dict) -> bool:
    """把策略調參的修改寫進 learned params（cur 會被修改）；不是本模組的修改回傳 False。"""
    p = ch["param"]
    if p == "strategy.portfolio":
        cur.setdefault("strategy", {}).update(ch["after"])
        off = copy.deepcopy(settings()["baskets"]["offense"])
        off.update(cur.get("baskets", {}).get("offense", {}))
        off["max_positions"] = ch["after"]["max_positions"]
        cur.setdefault("baskets", {})["offense"] = off
        return True
    if p in ("daytrade", "futures"):
        cur.setdefault(p, {}).update(ch["after"])
        return True
    return False
