"""RegimeAgent：大盤攻守燈號與撤資預警（數字全由程式計算，不經 LLM）。

燈號（加權指數日收盤，含遲滯避免來回切換；訊號統計 2021-04～2026-09 加權指數 1,318 個交易日）：
- 綠燈「強勢」：收盤 > 月線、> 季線 ×1.02、20 日漲幅 > 2%；收盤跌破月線 ×0.98 才解除
  強勢時未來 20 日平均 +1.92%（其他 +1.53%），20 日內跌逾 5% 機率 14.9%（其他 31.0%）→ 加重進攻
- 紅燈「轉弱」：收盤 < 季線 ×0.98；站回季線 ×1.01 才解除（優先於綠燈）
  跌破季線時未來 20 日平均 +0.73%（其他 +2.18%），20 日內跌逾 5% 機率 34.9%（其他 17.3%）→ 提早轉守
- 其餘為「中性」
攻守 β 代理（進攻 1.3、穩健 0.6）、換籃成本 0.6% 照算、訊號隔日生效的回測（2020-12～2026-10，1,399 個交易日）：
  固定 60/40 年化 25.4%、最大回檔 −32.2%、38 日平均 +3.47%、38 日收正 67.7%
  綠燈進攻 85%／中性 60%／紅燈 30% 年化 26.3%、最大回檔 −29.0%、38 日平均 +3.56%、38 日收正 63.3%
  （只加攻不轉守年化 27.6% 但回檔 −32.7%；過熱就減碼的舊規則年化 24.8%，已移除：RSI>70、20 日漲>8% 之後報酬反而較高）

黃燈「預警」（不改配置，先暫停追高、預先算好轉守點位）：以下條件之後 20 日內跌逾 5% 的機率明顯偏高，
但平均報酬沒有變差（常反彈），因此只提醒、不急著賣：
- VIX > 25：40.9%（其他 20.1%）
- 費城半導體前一晚跌 > 3%：34.3%（其他 21.8%）
- 逼近季線（距季線 0～2%）且月線下彎：31.1%（其他 22.3%）
- 報告中估計影響總資產 ≤ −1% 的國際事件（金額由程式依因子敏感度計算）
"""
from __future__ import annotations

import pandas as pd

STRONG_ENTER_MA60 = 1.02
STRONG_ENTER_R20 = 0.02
STRONG_EXIT_MA20 = 0.98
WEAK_ENTER_MA60 = 0.98
WEAK_EXIT_MA60 = 1.01
OFFENSE = {"強勢": 0.85, "中性": 0.60, "轉弱": 0.30}
LIGHT = {"強勢": "綠燈", "中性": "灰燈", "轉弱": "紅燈"}

BACKTEST = {
    "period": "2020-12～2026-10 加權指數 1,399 個交易日（訊號隔日生效、換籃成本 0.6% 照算；進攻 β1.3／穩健 β0.6 代理）",
    "baseline": {"name": "固定進攻 60%／穩健 40%", "cagr": 25.4, "max_dd": -32.2, "avg38": 3.47, "pos38": 67.7},
    "adopted": {"name": "綠燈進攻 85%／中性 60%／紅燈 30%", "cagr": 26.3, "max_dd": -29.0, "avg38": 3.56, "pos38": 63.3},
}


def states(close: pd.Series) -> pd.Series:
    """逐日燈號（只用當日以前的資料，遲滯規則與回測相同）。"""
    c = close.dropna()
    ma20, ma60 = c.rolling(20).mean(), c.rolling(60).mean()
    r20 = c.pct_change(20)
    out, strong, weak = [], False, False
    for i in range(len(c)):
        if pd.isna(ma60.iloc[i]):
            out.append("中性")
            continue
        p, m20, m60 = c.iloc[i], ma20.iloc[i], ma60.iloc[i]
        weak = (weak and not p > m60 * WEAK_EXIT_MA60) or (not weak and p < m60 * WEAK_ENTER_MA60)
        strong = (strong and not p < m20 * STRONG_EXIT_MA20) or (
            not strong and p > m20 and p > m60 * STRONG_ENTER_MA60 and r20.iloc[i] > STRONG_ENTER_R20)
        out.append("轉弱" if weak else "強勢" if strong else "中性")
    return pd.Series(out, index=c.index)


def _us_last(us_hist: dict | None, ticker: str) -> tuple[float | None, float | None]:
    """(最新收盤, 單日漲跌)。"""
    df = (us_hist or {}).get(ticker)
    if df is None or len(df) < 2:
        return None, None
    c = df.Close.dropna()
    return float(c.iloc[-1]), float(c.iloc[-1] / c.iloc[-2] - 1)


def assess(taiex: pd.DataFrame, us_hist: dict | None = None, live_price: float | None = None,
           events: list[dict] | None = None) -> dict:
    """目前燈號＋黃燈預警＋具體轉守／轉攻點位。live_price：盤中即時加權指數（用來預判今天收盤會不會變燈）。"""
    c = taiex.Close.dropna()
    st = states(c)
    state = st.iloc[-1]
    since = st[st != state].index.max()
    days_in = int((st.index > since).sum()) if pd.notna(since) else len(st)
    last = float(c.iloc[-1])
    ma20, ma60 = float(c.tail(20).mean()), float(c.tail(60).mean())
    ma20_prev = float(c.iloc[-25:-5].mean())
    r20 = float(c.iloc[-1] / c.iloc[-21] - 1)
    dist60 = last / ma60 - 1
    weak_line = round(ma60 * WEAK_ENTER_MA60)
    recover_line = round(ma60 * WEAK_EXIT_MA60)
    strong_exit_line = round(ma20 * STRONG_EXIT_MA20)
    strong_enter_line = round(max(ma20, ma60 * STRONG_ENTER_MA60))

    warns = []
    vix, _ = _us_last(us_hist, "^VIX")
    if vix and vix > 25:
        warns.append(f"VIX 恐慌指數 {vix:.1f} > 25（歷史上之後 20 日內跌逾 5% 的機率 40.9%，平常 20.1%）")
    _, sox = _us_last(us_hist, "^SOX")
    if sox is not None and sox < -0.03:
        warns.append(f"費城半導體前一晚 {sox * 100:+.1f}%（跌逾 3% 後 20 日內跌逾 5% 機率 34.3%，平常 21.8%）")
    if 0 <= dist60 <= 0.02 and ma20 < ma20_prev:
        warns.append(f"加權指數逼近季線（距季線 {dist60 * 100:+.1f}%）且月線下彎（之後 20 日內跌逾 5% 機率 31.1%，平常 22.3%）")
    for e in events or []:  # 報告中估計對總資產影響 ≤ −1% 的國際事件（金額由程式依因子敏感度計算）
        if (e.get("total_pct_nav") or 0) <= -1.0:
            warns.append(f"重大國際事件：{(e.get('event') or '')[:50]}（估計影響總資產 {e['total_pct_nav']:+.2f}%）")

    live = None
    if live_price:
        if state != "轉弱" and live_price < weak_line:
            live = f"盤中加權指數 {live_price:,.0f} 已低於轉弱線 {weak_line:,}，若收盤仍在線下，明天起轉為紅燈（轉守）"
        elif state == "強勢" and live_price < strong_exit_line:
            live = f"盤中加權指數 {live_price:,.0f} 已跌破月線 −2%（{strong_exit_line:,}），若收盤仍在線下，綠燈解除"
        elif state == "轉弱" and live_price > recover_line:
            live = f"盤中加權指數 {live_price:,.0f} 已站回季線 +1%（{recover_line:,}），若收盤守住，紅燈解除"
        if live:
            warns.insert(0, live)

    level = "紅燈" if state == "轉弱" else "黃燈" if warns else LIGHT[state]
    if state == "轉弱":
        head = f"紅燈：大盤轉弱（收盤 {last:,.0f} 低於季線 −2%），提早轉守"
        actions = ["暫停進攻籃新買進；進攻籃中跌破停損或走弱的持股優先賣出（持有未滿 10 天的停損照樣執行）",
                   f"新資金與換股改買穩健籃／ETF，進攻比例降到 {OFFENSE['轉弱'] * 100:.0f}%",
                   f"站回 {recover_line:,} 點（季線 +1%）才解除紅燈"]
    elif warns:
        head = f"黃燈：{LIGHT[state]}（{state}）但出現 {len(warns)} 項風險訊號，先暫停追高"
        actions = ["進攻籃暫停追高新買進（已持有的照停損停利操作，不急著賣，這類訊號之後常反彈）",
                   f"若收盤跌破 {weak_line:,} 點（季線 −2%）就轉紅燈：撤出走弱的進攻部位、改買穩健籃／ETF",
                   "期貨不加碼多單；已持有多單設好停損"]
    elif state == "強勢":
        head = f"綠燈：大盤強勢（站上月線與季線、20 日漲 {r20 * 100:+.1f}%），積極進攻"
        actions = [f"進攻比例提高到 {OFFENSE['強勢'] * 100:.0f}%，強勢時不因 RSI 高或漲多就減碼（回測顯示之後報酬反而較高）",
                   f"收盤跌破 {strong_exit_line:,} 點（月線 −2%）才解除綠燈"]
    else:
        head = f"灰燈：大盤中性（距季線 {dist60 * 100:+.1f}%、20 日 {r20 * 100:+.1f}%）"
        actions = [f"維持進攻 {OFFENSE['中性'] * 100:.0f}%／穩健 {100 - OFFENSE['中性'] * 100:.0f}%",
                   f"收盤站上 {strong_enter_line:,} 點且 20 日漲幅 > 2% 轉綠燈（加攻）；跌破 {weak_line:,} 點轉紅燈（轉守）"]
    return {"state": state, "light": LIGHT[state], "level": level, "days_in_state": days_in, "headline": head,
            "warnings": warns, "actions": actions, "offense": OFFENSE[state],
            "lines": {"close": round(last), "ma20": round(ma20), "ma60": round(ma60), "weak_enter": weak_line,
                      "weak_exit": recover_line, "strong_enter": strong_enter_line, "strong_exit": strong_exit_line},
            "r20_pct": round(r20 * 100, 2), "dist_ma60_pct": round(dist60 * 100, 2), "backtest": BACKTEST,
            "date": str(c.index[-1].date())}
