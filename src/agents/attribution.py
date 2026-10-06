"""AttributionAgent：解釋「為何今日（最近交易日）會漲／會跌」，並換算成金額。

報酬拆解：個股報酬 = 大盤因素（β × 加權指數報酬）＋ 產業因素（同產業中位數 − 大盤）＋ 個股特有因素（剩餘）
再補充：開盤跳空 vs 盤中走勢、隔夜美股、當日法人買賣超、量能、漲跌停、新聞事件。
全部數字由程式計算。
"""
from __future__ import annotations

import logging
import math

import pandas as pd

from src.agents.market_data import tw_institutional
from src.config import stock_meta

log = logging.getLogger(__name__)


def _beta(stock: pd.Series, mkt: pd.Series, n: int = 120) -> float:
    df = pd.concat([stock, mkt], axis=1, join="inner").dropna().tail(n)
    if len(df) < 30 or df.iloc[:, 1].var() == 0:
        return 1.0
    b = float(df.iloc[:, 0].cov(df.iloc[:, 1]) / df.iloc[:, 1].var())
    return b if math.isfinite(b) else 1.0


def attribute(codes_shares: dict[str, int], panel: dict[str, pd.DataFrame], taiex: pd.DataFrame,
              universe_codes: list[str], target: pd.Timestamp, us_snap_prev: dict[str, dict],
              senti_by_code: dict) -> dict:
    tx = taiex[taiex.index < target].dropna(subset=["Close"])  # 資料源偶有 NaN 列
    day = tx.index[-1]
    mkt_ret = float(tx.Close.iloc[-1] / tx.Close.iloc[-2] - 1)
    mret_series = tx.Close.pct_change()

    # 產業當日中位數報酬
    sector_rets: dict[str, list[float]] = {}
    rets_today: dict[str, float] = {}
    for c in universe_codes:
        df = panel.get(c)
        if df is None or day not in df.index:
            continue
        i = df.index.get_loc(day)
        if i < 1:
            continue
        r = float(df.Close.iloc[i] / df.Close.iloc[i - 1] - 1)
        rets_today[c] = r
        sector_rets.setdefault(stock_meta(c)["sector"], []).append(r)
    sector_med = {k: float(pd.Series(v).median()) for k, v in sector_rets.items() if len(v) >= 3}

    rows = []
    for code, shares in codes_shares.items():
        df = panel.get(code)
        if df is None or day not in df.index:
            continue
        df = df[df.index <= day].dropna(subset=["Close"])
        if len(df) < 2 or df.index[-1] != day:
            continue
        prev_c, c, o = float(df.Close.iloc[-2]), float(df.Close.iloc[-1]), float(df.Open.iloc[-1])
        if not (math.isfinite(o) and o > 0):
            o = c
        if not all(math.isfinite(x) and x > 0 for x in (prev_c, c)):
            log.warning("歸因略過 %s：價格缺值", code)
            continue
        ret = c / prev_c - 1
        meta = stock_meta(code)
        beta = _beta(df.Close.pct_change(), mret_series)
        mkt_part = beta * mkt_ret
        sec = meta["sector"]
        sec_others = [rets_today[x] for x in rets_today if x != code and stock_meta(x)["sector"] == sec]
        ind_med = float(pd.Series(sec_others).median()) if len(sec_others) >= 3 else None
        ind_part = (ind_med - mkt_ret) if ind_med is not None else 0.0
        idio = ret - mkt_part - ind_part
        pos_value_prev = shares * prev_c
        amt = lambda pct: round(pos_value_prev * pct)  # noqa: E731
        gap = o / prev_c - 1
        intra = c / o - 1
        vol_ratio = float(df.Volume.iloc[-1] / df.Volume.tail(21).head(20).mean())
        if not math.isfinite(vol_ratio):
            vol_ratio = 1.0
        inst = tw_institutional(code)
        inst_today = None
        if not inst.empty and pd.Timestamp(day) in inst.index:
            inst_today = inst.loc[pd.Timestamp(day)].to_dict()

        parts = [
            ("大盤因素", mkt_part, f"加權指數 {mkt_ret * 100:+.2f}%，此股對大盤敏感度 β = {beta:.2f}，"
                                  f"貢獻 {mkt_part * 100:+.2f}%（{amt(mkt_part):+,} 元）"),
        ]
        if ind_med is not None:
            parts.append(("產業因素", ind_part, f"{sec}類股中位數 {ind_med * 100:+.2f}%，相對大盤 {ind_part * 100:+.2f}%"
                                               f"（{amt(ind_part):+,} 元）"))
        idio_detail = []
        if inst_today:
            idio_detail.append(f"當日三大法人{'買超' if inst_today['total'] > 0 else '賣超'} {abs(inst_today['total']) / 1000:,.0f} 張"
                               f"（外資 {inst_today['foreign'] / 1000:+,.0f}、投信 {inst_today['trust'] / 1000:+,.0f}、"
                               f"自營商 {inst_today['dealer'] / 1000:+,.0f} 張）")
        idio_detail.append(f"成交量為 20 日均量的 {vol_ratio:.2f} 倍" + ("（爆量）" if vol_ratio > 2 else "（量縮）" if vol_ratio < 0.6 else ""))
        if abs(ret) >= 0.095:
            idio_detail.append("漲停" if ret > 0 else "跌停")
        if senti_by_code.get(code):
            idio_detail.append("相關新聞：" + "；".join(senti_by_code[code]["events"][:2]))
        parts.append(("個股特有因素", idio, f"剩餘 {idio * 100:+.2f}%（{amt(idio):+,} 元）：" + "；".join(idio_detail)))
        main = max(parts, key=lambda p: abs(p[1]))
        drivers = "、".join(f"{us_snap_prev[d]['name']} {us_snap_prev[d]['change_pct']:+.2f}%"
                            for d in meta.get("drivers", []) if d in us_snap_prev)
        structure = (f"走勢結構：開盤跳空 {gap * 100:+.2f}%（前一晚美股：{drivers or '無對應資料'}），"
                     f"開盤後至收盤 {intra * 100:+.2f}%"
                     + ("，開高走低" if gap > 0.005 and intra < -0.005 else "，開低走高" if gap < -0.005 and intra > 0.005 else ""))
        rows.append({
            "code": code, "name": meta["name"], "sector": sec, "shares": shares, "date": str(day.date()),
            "prev_close": round(prev_c, 2), "close": round(c, 2), "ret_pct": round(ret * 100, 2),
            "pnl": round(shares * (c - prev_c)), "beta": round(beta, 2),
            "mkt_pct": round(mkt_part * 100, 2), "ind_pct": round(ind_part * 100, 2), "idio_pct": round(idio * 100, 2),
            "gap_pct": round(gap * 100, 2), "intra_pct": round(intra * 100, 2), "vol_ratio": round(vol_ratio, 2),
            "main_driver": main[0], "explanations": [p[2] for p in sorted(parts, key=lambda p: -abs(p[1]))] + [structure],
            "verdict": (f"今日{'上漲' if ret > 0 else '下跌' if ret < 0 else '平盤'} {ret * 100:+.2f}%"
                        + (f"（{shares * (c - prev_c):+,.0f} 元）" if shares else "")
                        + f"，主因為「{main[0]}」（{main[1] * 100:+.2f}%）"),
        })
    total = sum(r["pnl"] for r in rows)
    return {"date": str(day.date()), "taiex_ret_pct": round(mkt_ret * 100, 2), "sector_median_pct":
            {k: round(v * 100, 2) for k, v in sorted(sector_med.items(), key=lambda kv: -kv[1])},
            "rows": rows, "total_pnl": total}
