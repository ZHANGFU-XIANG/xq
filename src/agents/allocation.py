"""AllocationAgent：動態決定「進攻籃 / 穩健籃 / ETF 籃」比例，並寫出每一項調整的理由。

三籃比例都是「股票＋ETF 目標持股市值」的分數，合計為 1；期貨另計（不算持股率）。
ETF 籃依基準比例（settings.allocation.etf）與大盤燈號調整，其餘部分再依下列訊號分給進攻／穩健：
- 大盤燈號（src/agents/regime.py，回測驗證）：綠燈強勢進攻 85%、灰燈中性 60%、紅燈轉弱 30%
  （2026-10-06 改：舊規則「過熱就減進攻」回測無益——RSI>70、20 日漲>8% 之後報酬反而較高，已移除）
- 黃燈預警（VIX>25、費半大跌、逼近季線且月線下彎、重大國際事件）：進攻最多 60%，先暫停追高
- 總資產自高點回撤：≥4% 進攻 −10%、≥8% 進攻 −20%
- 競賽尾聲（剩餘 ≤15 交易日）：已正報酬 → 進攻 −10% 保住晉級資格；仍虧損 → 進攻 +10% 爭取轉正
- 調整 Agent 偵測到重大失誤 → 進攻 −5%
"""
from __future__ import annotations

import pandas as pd

from src.agents import regime as regime_mod
from src.agents.alpha_model import _rsi
from src.state import params


def decide(taiex: pd.DataFrame, target: pd.Timestamp, history: list[dict], initial: float, nav: float,
           remaining_days: int, issues: list[dict], us_hist: dict | None = None, events: list[dict] | None = None) -> dict:
    cfg = params.allocation()
    tx_df = taiex[taiex.index < target]
    tx = tx_df.Close.dropna()
    rsi = float(_rsi(tx).iloc[-1])
    r20 = float(tx.iloc[-1] / tx.iloc[-21] - 1)
    dist60 = float(tx.iloc[-1] / tx.tail(60).mean() - 1)
    rg = regime_mod.assess(tx_df, us_hist, events=events)
    off = rg["offense"]
    bt = rg["backtest"]
    reasons = [f"{rg['headline']} → 進攻籃 {off * 100:.0f}%（已持續 {rg['days_in_state']} 個交易日）",
               "依據（報酬率第一）：十年回測每次前 20 名 20 日報酬，進攻籃在綠燈 6.0%／灰燈 6.2%／紅燈 3.7%，"
               "都高於穩健籃 4.4%／3.8%／2.1%，勝率相近"]
    tilt = round(cfg["base_offense"] - regime_mod.OFFENSE["中性"], 2)  # 調整 Agent 依兩籃實際表現學到的偏移
    if tilt:
        off += tilt
        reasons.append(f"調整 Agent：兩籃實際表現 → 進攻 {tilt * 100:+.0f}%")
    if rg["level"] == "黃燈" and off > regime_mod.OFFENSE["中性"]:
        off = regime_mod.OFFENSE["中性"]
        reasons.append(f"黃燈預警 → 進攻最多 {off * 100:.0f}%，先暫停追高：" + "；".join(rg["warnings"][:3]))

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
    if rg["state"] == "轉弱":
        e += 0.10
        etf_reasons.append("紅燈轉弱 → ETF +10%（分散個股下跌風險）")
    elif rg["state"] == "強勢" and rg["level"] != "黃燈" and not issues:
        e -= 0.05
        etf_reasons.append("綠燈強勢 → ETF −5%，資金優先給進攻個股")
    if history:
        peak = max(max(h["nav"] for h in history), initial)
        if cfg["drawdown_steps"] and 1 - nav / peak >= cfg["drawdown_steps"][0][0]:
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
            "taiex": {"rsi14": round(rsi, 1), "r20_pct": round(r20 * 100, 2), "dist_ma60_pct": round(dist60 * 100, 2)},
            "regime": rg}
