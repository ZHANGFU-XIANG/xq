"""AllocationAgent：動態決定「進攻籃 / 穩健籃」比例，並寫出每一項調整的理由。

基準 進攻 60% / 穩健 40%（佔目標持股市值），依以下訊號加減，限制在 40%～70%：
- 大盤過熱（RSI、20 日漲幅、距季線）：每項成立進攻 −5%
- 大盤健康上漲（站上季線、20 日漲幅為正）且調整 Agent 無重大失誤：進攻 +5%
- 總資產自高點回撤：≥4% 進攻 −10%、≥8% 進攻 −20%
- 競賽尾聲（剩餘 ≤15 交易日）：已正報酬 → 進攻 −10% 保住晉級資格；仍虧損 → 進攻 +10% 爭取轉正
- 調整 Agent 偵測到重大失誤 → 進攻 −5%
"""
from __future__ import annotations

import pandas as pd

from src.agents.alpha_model import _rsi
from src.state import params


def decide(taiex: pd.DataFrame, target: pd.Timestamp, history: list[dict], initial: float, nav: float,
           remaining_days: int, issues: list[dict]) -> dict:
    cfg = params.allocation()
    off = cfg["base_offense"]
    reasons = [f"基準配置：進攻籃 {cfg['base_offense'] * 100:.0f}%、穩健籃 {(1 - cfg['base_offense']) * 100:.0f}%"]
    tx = taiex[taiex.index < target].Close
    rsi = float(_rsi(tx).iloc[-1])
    r20 = float(tx.iloc[-1] / tx.iloc[-21] - 1)
    ma60 = float(tx.tail(60).mean())
    dist60 = float(tx.iloc[-1] / ma60 - 1)
    oh = cfg["overheat"]
    hot = []
    if rsi > oh["taiex_rsi"]:
        hot.append(f"加權指數 RSI14 = {rsi:.1f} > {oh['taiex_rsi']}")
    if r20 > oh["taiex_r20"]:
        hot.append(f"加權指數 20 日漲幅 {r20 * 100:+.1f}% > {oh['taiex_r20'] * 100:.0f}%")
    if dist60 > oh["taiex_dist_ma60"]:
        hot.append(f"加權指數高於季線 {dist60 * 100:+.1f}% > {oh['taiex_dist_ma60'] * 100:.0f}%")
    if hot:
        off -= 0.05 * len(hot)
        reasons.append(f"大盤過熱 → 進攻 −{5 * len(hot)}%：" + "；".join(hot) + "（高檔追價風險升高，提高穩健部位）")
    elif dist60 > 0 and r20 > cfg["strong"]["taiex_r20_min"] and not issues:
        off += 0.05
        reasons.append(f"大盤健康上漲 → 進攻 +5%：站上季線 {dist60 * 100:+.1f}%、20 日漲幅 {r20 * 100:+.1f}%，且模型無重大失誤")
    else:
        reasons.append(f"大盤中性（RSI14 {rsi:.1f}、20 日 {r20 * 100:+.1f}%、距季線 {dist60 * 100:+.1f}%）→ 不調整")

    if history:
        peak = max(max(h["nav"] for h in history), initial)
        dd = 1 - nav / peak
        for lvl, cut in sorted(cfg["drawdown_steps"], reverse=True):
            if dd >= lvl:
                off -= cut
                reasons.append(f"總資產自高點回撤 {dd * 100:.1f}% ≥ {lvl * 100:.0f}% → 進攻 −{cut * 100:.0f}%，先止血")
                break

    cum = nav / initial - 1
    if remaining_days <= cfg["late_days"]:
        if cum > 0:
            off -= 0.10
            reasons.append(f"競賽剩 {remaining_days} 個交易日且累積報酬 {cum * 100:+.2f}% 為正 → 進攻 −10%，"
                           f"優先保住「正報酬才排名」的晉級資格")
        else:
            off += 0.10
            reasons.append(f"競賽剩 {remaining_days} 個交易日但累積報酬 {cum * 100:+.2f}% 仍為負 → 進攻 +10%，"
                           f"維持負報酬就無法排名，需要爭取轉正")

    if issues:
        off -= 0.05
        reasons.append(f"調整 Agent 偵測到 {len(issues)} 項失誤 → 進攻 −5%，待模型修正後再恢復")

    clipped = min(cfg["max_offense"], max(cfg["min_offense"], off))
    if abs(clipped - off) > 1e-9:
        reasons.append(f"限制在進攻 {cfg['min_offense'] * 100:.0f}%～{cfg['max_offense'] * 100:.0f}% 範圍內")
    off = round(clipped, 2)
    reasons.append(f"股票內部：進攻籃 {off * 100:.0f}%、穩健籃 {(1 - off) * 100:.0f}%")

    # ETF 籃佔「股票＋ETF」目標持股的比例
    ecfg = cfg["etf"]
    e = ecfg["base"]
    etf_reasons = [f"ETF 基準比例 {ecfg['base'] * 100:.0f}%（分散個股風險、證交稅 0.1%）"]
    if hot:
        e += 0.05 * len(hot)
        etf_reasons.append(f"大盤過熱 {len(hot)} 項 → ETF +{5 * len(hot)}%（以分散標的降低個股追高風險）")
    elif dist60 > 0 and r20 > cfg["strong"]["taiex_r20_min"] and not issues:
        e -= 0.05
        etf_reasons.append("大盤健康上漲 → ETF −5%，資金優先給個股")
    if history:
        peak = max(max(h["nav"] for h in history), initial)
        if 1 - nav / peak >= cfg["drawdown_steps"][0][0]:
            e += 0.10
            etf_reasons.append(f"總資產回撤 {(1 - nav / peak) * 100:.1f}% → ETF +10%，降低個股波動")
    if remaining_days <= cfg["late_days"] and cum > 0:
        e += 0.10
        etf_reasons.append("競賽末段且已是正報酬 → ETF +10%，以穩定為主保住名次")
    e_clip = round(min(ecfg["max"], max(ecfg["min"], e)), 2)
    if abs(e_clip - e) > 1e-9:
        etf_reasons.append(f"限制在 ETF {ecfg['min'] * 100:.0f}%～{ecfg['max'] * 100:.0f}% 範圍內")
    e = e_clip
    etf_reasons.append(f"今日 ETF 籃比例 {e * 100:.0f}%（佔股票＋ETF 目標持股；基準 {ecfg['base'] * 100:.0f}% 已依上述條件調整）")
    stock = 1 - e
    reasons.append(f"今日配置（佔股票＋ETF 目標持股）：進攻籃 {off * stock * 100:.0f}%、穩健籃 {(1 - off) * stock * 100:.0f}%、"
                   f"ETF 籃 {e * 100:.0f}%")
    return {"offense": round(off * stock, 3), "defense": round((1 - off) * stock, 3), "etf": e,
            "stock_split": {"offense": off, "defense": round(1 - off, 2)}, "reasons": reasons, "etf_reasons": etf_reasons,
            "taiex": {"rsi14": round(rsi, 1), "r20_pct": round(r20 * 100, 2), "dist_ma60_pct": round(dist60 * 100, 2)}}
