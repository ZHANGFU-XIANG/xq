"""RecoveryAgent：虧損如何補回 —— 續抱、加碼、減碼、換股或停損，逐檔比較並說明理由。

比較基準：一個月模型的預期報酬（exp_ret）與創新高機率；換股需扣除換股成本
（賣出證交稅＋雙邊手續費），且新標的預期報酬需高出原標的 swap_margin 以上才換。
"""
from __future__ import annotations

from src.agents.sizer import round_trip_cost_pct
from src.state import params
from src.state.portfolio import Portfolio


def _months(need: float, monthly: float) -> str:
    if monthly <= 0:
        return "依目前模型預期無法回本"
    m = need / monthly
    return f"約 {m * 20:.0f} 個交易日" if m <= 3 else f"約 {m:.1f} 個月（超過競賽剩餘期間的風險高）"


def plan(pf: Portfolio, prices: dict, scores: dict, alpha_preds: dict, meta: dict, ranked: list[dict],
         nav: float, initial: float, today_pnl: dict[str, float], basket_of: dict | None = None) -> dict:
    st = params.strategy()
    basket_of = basket_of or {}
    held = set(pf.positions)
    off_alts = [s for s in ranked if s["code"] not in held and s.get("alpha") and s.get("signal") == "買進候選"]
    def_alts = [s for s in ranked if s["code"] not in held and s.get("alpha") and s.get("def_signal") == "買進候選"]
    best = {"offense": max(off_alts, key=lambda s: s["alpha"]["exp_ret"], default=None),
            "defense": max(def_alts, key=lambda s: (s["alpha"]["p_up"], s["alpha"]["exp_ret"]), default=None)}
    prob_key = {"offense": ("p_newhigh", "創新高機率"), "defense": ("p_up", "收正報酬機率")}
    items, actions = [], {}

    for code, pos in pf.positions.items():
        basket = basket_of.get(code, "offense")
        best_alt = best[basket]
        pk, pname = prob_key[basket]
        price = prices.get(code)
        if not price:
            continue
        s = scores.get(code, {})
        a = alpha_preds.get(code)
        mv = pos.shares * price
        unreal = mv - pos.cost_basis
        pnl_pct = unreal / pos.cost_basis if pos.cost_basis else 0.0
        stop = meta.get(code, {}).get("stop")
        hold_exp = a["exp_ret"] if a else None
        hold_p = a[pk] if a else None
        switch = round_trip_cost_pct(code) / 100
        lines, decision = [], "續抱"

        lines.append(f"目前市值 {mv:,.0f} 元，成本 {pos.cost_basis:,.0f} 元，未實現損益 {unreal:+,.0f} 元（{pnl_pct * 100:+.2f}%）"
                     + (f"；今日損益 {today_pnl.get(code, 0):+,.0f} 元" if code in today_pnl else ""))
        if a:
            lines.append(f"【{params.basket(basket)['name']}】續抱的預期：一個月預期報酬 {hold_exp * 100:+.2f}%"
                         f"（約 {mv * hold_exp:+,.0f} 元），{pname} {hold_p * 100:.1f}%")
        else:
            lines.append("此檔不在流動性股票池內，無一個月模型預測，僅依技術面與停損管理")

        if unreal < 0:
            need = -unreal / mv
            lines.append(f"補回虧損需上漲 {need * 100:.2f}%；續抱依模型預期需 " + (_months(need, hold_exp) if a else "無法估算"))
            if best_alt:
                alt_exp = best_alt["alpha"]["exp_ret"]
                net_mv = mv * (1 - switch)
                need_alt = (-unreal + mv * switch) / net_mv
                lines.append(f"換股方案（同一籃）：賣出後換入 {best_alt['code']} {best_alt['name']}（一個月預期報酬 {alt_exp * 100:+.2f}%、"
                             f"{pname} {best_alt['alpha'][pk] * 100:.1f}%），扣除換股成本 {switch * 100:.3f}%"
                             f"（約 {mv * switch:,.0f} 元）後，需上漲 {need_alt * 100:.2f}%，需 {_months(need_alt, alt_exp)}")

        # ---- 決策
        if stop and price <= stop:
            decision = "停損換股"
            why = f"收盤 {price} 已跌破停損價 {stop}，依紀律出場；資金轉往評分最高的候選標的"
        elif a and best_alt and (best_alt["alpha"]["exp_ret"] - switch - hold_exp) > st["swap_margin"] \
                and best_alt["alpha"][pk] >= hold_p:
            decision = "換股"
            gain = best_alt["alpha"]["exp_ret"] - switch - hold_exp
            why = (f"{best_alt['code']} {best_alt['name']} 扣除換股成本後的預期報酬比續抱高 {gain * 100:.2f}%"
                   f"（門檻 {st['swap_margin'] * 100:.1f}%），且{pname}不低於原標的 → 換股補回虧損較快；"
                   f"回收資金依綜合分數配置到買進候選，各檔仍受單一標的與產業上限約束")
        elif (s.get("def_score", 0) if basket == "defense" else s.get("score", 0)) <= st["sell_score_threshold"]:
            decision = "減碼"
            why = "分數已低於賣出門檻，先減碼一半降低風險，資金轉往同一籃的高分標的"
        elif a and hold_exp > 0 and a["def_combo" if basket == "defense" else "combo"] >= 0.8 and pnl_pct > -0.05 and mv / nav < st["max_weight"] * 0.8 \
                and (s.get("features") or {}).get("close", 0) > (s.get("features") or {}).get("ma20", 1e18):
            decision = "續抱並可加碼"
            why = (f"模型排名仍在全市場前 {(1 - a['def_combo' if basket == 'defense' else 'combo']) * 100:.0f}%、股價在月線之上、虧損未擴大，"
                   f"權重 {mv / nav * 100:.1f}% 仍低於上限，可逢回加碼攤低成本")
        else:
            why = ("模型預期仍為正，未觸及停損，換股的預期效益不足以支付換股成本" if (a and hold_exp > 0)
                   else "未觸及停損，但模型預期偏弱，密切觀察；若跌破停損即出場")
        lines.append(f"決策：{decision} —— {why}")
        items.append({"code": code, "name": s.get("name", code), "basket": basket,
                      "unrealized": round(unreal), "pnl_pct": round(pnl_pct * 100, 2),
                      "decision": decision, "why": why, "lines": lines, "exp_ret": hold_exp, "p_newhigh": hold_p,
                      "alt": {"code": best_alt["code"], "name": best_alt["name"]} if best_alt and decision in ("換股", "停損換股") else None})
        if decision in ("換股", "停損換股", "減碼"):
            actions[code] = {"decision": decision, "why": why,
                             "alt": f"{best_alt['code']} {best_alt['name']}" if best_alt else None}

    # ---- 投資組合層級
    cum = nav - initial
    port_exp = sum(it["exp_ret"] * pf.positions[it["code"]].shares * prices[it["code"]]
                   for it in items if it["exp_ret"] is not None) / nav if nav else 0
    summary = [f"累積損益 {cum:+,.0f} 元（{cum / initial * 100:+.2f}%），已實現 {pf.realized_pnl:+,.0f} 元"]
    if cum < 0:
        summary.append(f"回到初始資金需上漲 {-cum / nav * 100:.2f}%；只有正報酬才列入排名，補回虧損是第一優先")
    if items:
        summary.append(f"現有持股依模型加權的一個月預期報酬 {port_exp * 100:+.2f}%（約 {port_exp * nav:+,.0f} 元）")
    for b, alt in best.items():
        if alt:
            pk, pname = prob_key[b]
            summary.append(f"{params.basket(b)['name']}最佳替換候選：{alt['code']} {alt['name']}，一個月預期報酬 "
                           f"{alt['alpha']['exp_ret'] * 100:+.2f}%，{pname} {alt['alpha'][pk] * 100:.1f}%")
    return {"items": items, "actions": actions, "summary": summary, "portfolio_exp_ret": round(port_exp, 4)}
