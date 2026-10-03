"""TunerAgent（調整 Agent）：追蹤每次預測的對錯，偵測重大失誤，並修正模型／演算法參數。

1. 記錄：每次盤前把隔夜預測（大盤與個股的開盤跳空、開盤後走勢）與一個月模型預測寫入 data/prediction_log.csv
2. 對答案：資料到齊後自動計算實際結果
3. 健康檢查：隔夜模型命中率、大盤重大誤判、一個月推薦的實際超額報酬、總資產回撤、單日重大虧損
4. 修正（只在觸發或每週例行時）：
   - 一個月模型：嘗試多組超參數＋近期加權，滾動回測分數需比現行進步 accept_improvement 才採用
   - 隔夜模型：比較不同 Ridge 正則化與歷史長度，近 60 日命中率較佳才採用
   - 風控：回撤過大時下修每筆風險與單一標的上限；恢復後逐步回到預設
   - 評分權重：依各因子與實際報酬的相關性（IC），由最差因子移 0.05 權重給最佳因子
5. 每次修改都寫入 data/tuning_log.json（含修改前完整參數，可回復），不得修改競賽規則
"""
from __future__ import annotations

import copy
import csv
import datetime as dt
import json
import logging

import numpy as np
import pandas as pd

from src.config import DATA_DIR, settings
from src.state import params
from src.state.portfolio import load_history

log = logging.getLogger(__name__)
LOG_CSV = DATA_DIR / "prediction_log.csv"
TUNE_LOG = DATA_DIR / "tuning_log.json"
FIELDS = ["run_date", "target_date", "kind", "code", "pred", "band", "extra"]


# ---------------------------------------------------------------- 記錄
def log_predictions(run_date: dt.date, target: dt.date, overnight: dict, alpha_out: dict, ranked: list[dict],
                    holdings: list[str]) -> None:
    rows = []
    for code, p in overnight.items():
        rows.append({"kind": "gap", "code": code, "pred": p["gap_pct"], "band": p["gap_band_pct"]})
        rows.append({"kind": "intra", "code": code, "pred": p["intraday_pct"], "band": p.get("intra_band_pct")})
    k = params.alpha()["top_k"]
    off = [s["code"] for s in ranked if s.get("alpha") and s.get("signal") == "買進候選"][:k]
    dfn = [s["code"] for s in sorted(ranked, key=lambda x: -x.get("def_score", -9)) if s.get("def_signal") == "買進候選"][:k]
    for s in ranked:
        if not s.get("alpha"):
            continue
        for b, ok in (("offense", s["code"] in off or s["code"] in holdings), ("defense", s["code"] in dfn)):
            if not ok:
                continue
            rows.append({"kind": "alpha", "code": s["code"], "pred": s["alpha"]["exp_excess"], "band": s["alpha"]["p_newhigh"],
                         "extra": json.dumps({"hi252": s["alpha"]["hi252"], "close": s["alpha"]["close"], "basket": b,
                                              "p_up": s["alpha"]["p_up"], "components": s["components"],
                                              "score": s["score"]})})
    existing = load_log()
    keep = [r for r in existing if not (r["run_date"] == run_date.isoformat())]
    for r in rows:
        r.update({"run_date": run_date.isoformat(), "target_date": target.isoformat()})
    with LOG_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in keep + rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def load_log() -> list[dict]:
    if not LOG_CSV.exists():
        return []
    with LOG_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------- 對答案
def evaluate(panel: dict[str, pd.DataFrame], taiex: pd.DataFrame) -> dict:
    rows = load_log()
    horizon = params.alpha()["horizon"]
    over, alpha = [], []
    med_cache: dict[tuple, float] = {}
    for r in rows:
        df = taiex if r["code"] == "TAIEX" else panel.get(r["code"])
        if df is None:
            continue
        t = pd.Timestamp(r["target_date"])
        if t not in df.index:
            continue
        i = df.index.get_loc(t)
        if i < 1:
            continue
        pred = float(r["pred"])
        if r["kind"] in ("gap", "intra"):
            real = (df.Open.iloc[i] / df.Close.iloc[i - 1] - 1) if r["kind"] == "gap" else (df.Close.iloc[i] / df.Open.iloc[i] - 1)
            real *= 100
            over.append({**r, "real": float(real), "hit": (np.sign(real) == np.sign(pred)) if abs(real) > 0.1 else None,
                         "err_bands": abs(real - pred) / max(float(r["band"] or 1), 0.1)})
        elif r["kind"] == "alpha" and i + horizon - 1 < len(df):
            ex = json.loads(r["extra"])
            win = df.iloc[i:i + horizon]
            ret = float(win.Close.iloc[-1] / df.Close.iloc[i - 1] - 1)
            key = (r["target_date"],)
            if key not in med_cache:  # 同期間全市場中位數報酬
                rs = []
                for d in panel.values():
                    if t in d.index:
                        j = d.index.get_loc(t)
                        if j >= 1 and j + horizon - 1 < len(d):
                            rs.append(d.Close.iloc[j + horizon - 1] / d.Close.iloc[j - 1] - 1)
                med_cache[key] = float(np.median(rs)) if rs else 0.0
            alpha.append({**r, "real_ret": ret, "real_excess": ret - med_cache[key], "basket": ex.get("basket", "offense"),
                          "real_newhigh": float(win.High.max() > ex["hi252"]), "components": ex["components"]})
    return {"overnight": over, "alpha": alpha}


# ---------------------------------------------------------------- 健康檢查
def health(ev: dict, nav_today: float | None = None, today_pnls: dict[str, float] | None = None) -> list[dict]:
    cfg = settings()["tuner"]
    issues = []
    tx = sorted([o for o in ev["overnight"] if o["code"] == "TAIEX" and o["kind"] == "gap"], key=lambda o: o["target_date"])
    recent = [o for o in tx[-cfg["overnight_window"]:] if o["hit"] is not None]
    if len(recent) >= 5:
        hit = np.mean([o["hit"] for o in recent])
        if hit < cfg["overnight_min_hit"]:
            issues.append({"type": "overnight", "severity": "高", "detail": f"大盤隔夜跳空預測近 {len(recent)} 次命中率 {hit * 100:.0f}%，"
                                                                              f"低於 {cfg['overnight_min_hit'] * 100:.0f}%"})
    if tx and tx[-1]["err_bands"] > cfg["big_miss_bands"]:
        o = tx[-1]
        issues.append({"type": "overnight", "severity": "高", "detail": f"{o['target_date']} 大盤跳空預測 {float(o['pred']):+.2f}%，"
                                                                         f"實際 {o['real']:+.2f}%，誤差超過 {cfg['big_miss_bands']:.0f} 倍誤差帶"})
    mat = [a for a in ev["alpha"] if a.get("basket", "offense") == "offense"]
    if len(mat) >= cfg["alpha_min_matured"]:
        ex = np.mean([a["real_excess"] for a in mat[-60:]])
        nh = np.mean([a["real_newhigh"] for a in mat[-60:]])
        pnh = np.mean([float(a["band"]) for a in mat[-60:]])
        if ex < cfg["alpha_min_excess"]:
            issues.append({"type": "alpha", "severity": "高", "detail": f"一個月推薦標的實際平均超額報酬 {ex * 100:+.2f}%，"
                                                                         f"低於 {cfg['alpha_min_excess'] * 100:+.1f}%"})
        if nh < pnh * 0.6:
            issues.append({"type": "alpha", "severity": "中", "detail": f"推薦標的實際創新高比例 {nh * 100:.0f}%，"
                                                                         f"遠低於模型預估 {pnh * 100:.0f}%（機率高估）"})
    hist = load_history()
    if hist:
        peak = max(h["nav"] for h in hist)
        cur = nav_today or hist[-1]["nav"]
        dd = 1 - cur / peak
        if dd > cfg["max_drawdown"]:
            issues.append({"type": "drawdown", "severity": "高", "detail": f"總資產自高點 {peak:,.0f} 回落 {dd * 100:.1f}%，"
                                                                            f"超過 {cfg['max_drawdown'] * 100:.0f}%"})
    if today_pnls and nav_today:
        for code, pnl in today_pnls.items():
            if pnl < -cfg["big_loss_nav"] * nav_today:
                issues.append({"type": "big_loss", "severity": "高", "detail": f"{code} 單日虧損 {pnl:,.0f} 元，"
                                                                                f"超過總資產 {cfg['big_loss_nav'] * 100:.1f}%"})
    return issues


# ---------------------------------------------------------------- 修正
def _bounded(v: float, key: str) -> float:
    lo, hi = settings()["tuner"]["bounds"][key]
    return float(min(hi, max(lo, v)))


def tune_alpha(data_builder) -> dict | None:
    """嘗試多組超參數；在同一份資料、同一組回測區間上比較，分數需比現行進步才採用。"""
    from src.agents import alpha_model

    data = data_builder()
    cur_hp = {**alpha_model.DEFAULT_HP, **params.get("alpha").get("hyperparams", {})}
    base = alpha_model.walk_forward(data, cur_hp)["score"]
    trials = [
        {**cur_hp, "half_life_days": 180}, {**cur_hp, "half_life_days": 365},
        {**cur_hp, "learning_rate": 0.03, "max_iter": 400}, {**cur_hp, "max_leaf_nodes": 15, "l2": 3.0},
        {**cur_hp, "max_leaf_nodes": 63, "min_leaf": 400},
    ]
    best_hp, best = None, base
    results = []
    for hp in trials:
        hp = {k: v for k, v in hp.items() if k in alpha_model.DEFAULT_HP}
        sc = alpha_model.walk_forward(data, hp)["score"]
        results.append({"hp": hp, "score": round(sc, 4)})
        if sc > best:
            best_hp, best = hp, sc
    need = abs(base) * settings()["tuner"]["accept_improvement"]
    if best_hp and best - base > need:
        return {"param": "alpha.hyperparams", "before": cur_hp, "after": best_hp,
                "metric_before": round(base, 4), "metric_after": round(best, 4), "trials": results}
    return None


def tune_overnight(taiex: pd.DataFrame, us_hist: dict, target: dt.date) -> dict | None:
    from src.agents import predictor

    cur = params.predictor()
    cands = [(a, y) for a in (1.0, 5.0, 20.0) for y in (1, 2, 3)]
    scores = {}
    for a, y in cands:
        scores[(a, y)] = predictor.recent_hit_rate(taiex, us_hist, target, ridge_alpha=a, years=y, window=60)
    base = scores.get((cur["ridge_alpha"], cur["history_years"]))
    if base is None:
        base = predictor.recent_hit_rate(taiex, us_hist, target, ridge_alpha=cur["ridge_alpha"],
                                         years=cur["history_years"], window=60)
    (a, y), best = max(scores.items(), key=lambda kv: kv[1])
    if best - base > max(0.03, base * settings()["tuner"]["accept_improvement"]):
        return {"param": "predictor", "before": {"ridge_alpha": cur["ridge_alpha"], "history_years": cur["history_years"]},
                "after": {"ridge_alpha": a, "history_years": y}, "metric_before": round(base, 3), "metric_after": round(best, 3)}
    return None


def tune_risk(issues: list[dict]) -> list[dict]:
    st = params.strategy()
    default = settings()["strategy"]
    changes = []
    if any(i["type"] in ("drawdown", "big_loss") for i in issues):
        for key, f in (("risk_per_trade", 0.8), ("max_weight", None)):
            before = st[key]
            after = _bounded(before * f if f else before - 0.05, key)
            if after != before:
                changes.append({"param": f"strategy.{key}", "before": before, "after": round(after, 4),
                                "reason": "回撤或重大虧損 → 降低單筆風險與集中度"})
    elif not issues:
        for key in ("risk_per_trade", "max_weight"):
            before, d = st[key], default[key]
            if abs(before - d) > 1e-6:
                after = round(before + (d - before) * 0.25, 4)
                changes.append({"param": f"strategy.{key}", "before": before, "after": after,
                                "reason": "近期無重大失誤 → 逐步恢復預設風險"})
    return changes


def tune_allocation(ev: dict) -> dict | None:
    """依兩籃到期推薦的實際表現微調基準進攻比例（每次 ±5%，限制在 min~max）。"""
    cfg = params.allocation()
    need = settings()["tuner"]["alpha_min_matured"]
    off = [a["real_ret"] for a in ev["alpha"] if a.get("basket") == "offense"][-60:]
    dfn = [a["real_ret"] for a in ev["alpha"] if a.get("basket") == "defense"][-60:]
    if len(off) < need or len(dfn) < need:
        return None
    diff = float(np.mean(off) - np.mean(dfn))
    before = cfg["base_offense"]
    if diff > 0.03:
        after, why = before + 0.05, f"進攻籃實際平均報酬比穩健籃高 {diff * 100:.2f}%，提高進攻比例"
    elif diff < -0.01:
        after, why = before - 0.05, f"進攻籃實際平均報酬反而比穩健籃低 {-diff * 100:.2f}%，追高沒有回報，降低進攻比例"
    else:
        return None
    after = round(min(cfg["max_offense"], max(cfg["min_offense"], after)), 2)
    if after == before:
        return None
    return {"param": "allocation.base_offense", "before": before, "after": after, "reason": why,
            "metric_before": round(float(np.mean(dfn)), 4), "metric_after": round(float(np.mean(off)), 4)}


def tune_weights(ev: dict) -> dict | None:
    mat = [a for a in ev["alpha"] if a.get("basket", "offense") == "offense"]
    if len(mat) < settings()["tuner"]["alpha_min_matured"]:
        return None
    df = pd.DataFrame([{**a["components"], "y": a["real_excess"]} for a in mat[-120:]])
    ics = {c: float(df[c].corr(df["y"], method="spearman")) for c in df.columns if c != "y" and df[c].std() > 0}
    if len(ics) < 2:
        return None
    best, worst = max(ics, key=ics.get), min(ics, key=ics.get)
    if ics[best] - ics[worst] < 0.1:
        return None
    w = dict(params.get("score_weights"))
    before = dict(w)
    move = min(0.05, w[worst] - settings()["tuner"]["bounds"]["score_weight"][0])
    if move <= 0:
        return None
    w[worst] = round(w[worst] - move, 3)
    w[best] = round(_bounded(w[best] + move, "score_weight"), 3)
    return {"param": "score_weights", "before": before, "after": w, "ic": {k: round(v, 3) for k, v in ics.items()},
            "reason": f"「{best}」與實際報酬相關性最高（IC {ics[best]:+.2f}），「{worst}」最低（IC {ics[worst]:+.2f}）"}


def apply(changes: list[dict], issues: list[dict], today: dt.date, note: str = "") -> None:
    if not changes:
        _append_log({"date": today.isoformat(), "issues": issues, "changes": [], "note": note or "無需調整"})
        return
    cur = params.learned()
    snapshot = copy.deepcopy(cur)
    for ch in changes:
        p = ch["param"]
        if p == "alpha.hyperparams":
            cur.setdefault("alpha", {})["hyperparams"] = ch["after"]
        elif p == "predictor":
            cur.setdefault("predictor", {}).update(ch["after"])
        elif p == "score_weights":
            cur["score_weights"] = ch["after"]
        elif p == "allocation.base_offense":
            cur.setdefault("allocation", {})["base_offense"] = ch["after"]
        elif p.startswith("strategy."):
            cur.setdefault("strategy", {})[p.split(".", 1)[1]] = ch["after"]
    params.save_learned(cur)
    _append_log({"date": today.isoformat(), "issues": issues, "changes": changes, "previous_learned": snapshot, "note": note})


def _append_log(entry: dict) -> None:
    """寫入調整紀錄。「無需調整」的健康檢查同一天只留一筆（累計檢查次數），不會重複出現；
    當天已有實際修改時，健康檢查只計次、不另外新增「無需調整」。"""
    log_ = json.loads(TUNE_LOG.read_text(encoding="utf-8")) if TUNE_LOG.exists() else []
    if not entry.get("changes") and not entry.get("issues"):
        same_day = [e for e in log_ if e.get("date") == entry["date"]]
        target = next((e for e in reversed(same_day) if e.get("changes")), None) or             next((e for e in reversed(same_day) if not e.get("changes") and not e.get("issues")), None)
        if target is not None:
            target["checks"] = target.get("checks", 1) + 1
            TUNE_LOG.write_text(json.dumps(log_, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            return
    log_.append({**entry, "checks": 1})
    TUNE_LOG.write_text(json.dumps(log_, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def history() -> list[dict]:
    return json.loads(TUNE_LOG.read_text(encoding="utf-8")) if TUNE_LOG.exists() else []


def rollback() -> str:
    for entry in reversed(history()):
        if entry.get("changes"):
            params.save_learned(entry.get("previous_learned") or {})
            _append_log({"date": dt.date.today().isoformat(), "issues": [], "changes": [],
                         "note": f"回復到 {entry['date']} 調整前的參數"})
            return entry["date"]
    return ""


def summary_stats(ev: dict) -> dict:
    tx = [o for o in ev["overnight"] if o["code"] == "TAIEX" and o["kind"] == "gap" and o["hit"] is not None]
    stk = [o for o in ev["overnight"] if o["code"] != "TAIEX" and o["kind"] == "gap" and o["hit"] is not None]
    al = ev["alpha"]
    return {
        "taiex_gap_n": len(tx), "taiex_gap_hit": round(float(np.mean([o["hit"] for o in tx])), 3) if tx else None,
        "stock_gap_n": len(stk), "stock_gap_hit": round(float(np.mean([o["hit"] for o in stk])), 3) if stk else None,
        "alpha_n": len(al), "alpha_excess": round(float(np.mean([a["real_excess"] for a in al])), 4) if al else None,
        "alpha_newhigh": round(float(np.mean([a["real_newhigh"] for a in al])), 3) if al else None,
    }
