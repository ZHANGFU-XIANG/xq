"""RecoveryAgent：虧損如何補回 —— 續抱、加碼、減碼、換股或停損，逐檔比較並說明理由。

比較基準：一個月模型的預期報酬（exp_ret）與創新高機率；換股需扣除換股成本
（賣出證交稅＋雙邊手續費），且新標的預期報酬需高出原標的 swap_margin 以上才換。

結單轉股（2026-10-12，傳入 alts＝綜合排名時）：替代標的改用全市場綜合排名（含資訊事件×可信度、美股重挫），
條件依十年投資組合模擬（見 settings.strategy）：持有 ≥ min_hold_days 且扣成本後多 swap_margin、
或獲利 ≥ profit_swap_min_pnl 且多 profit_swap_margin（當天可結單）、或持有滿 expiry_days 且仍有正優勢。
一律全部賣出 A、同日買進 B；換完會讓持股率掉到 70% 以下（且比換前更低）或違反最後一日規定就不建議。
"""
from __future__ import annotations

from src.agents.sizer import round_trip_cost_pct
from src.state import params
from src.config import now_tw
from src.state.portfolio import Portfolio, held_days


def _months(need: float, monthly: float) -> str:
    if monthly <= 0:
        return "依目前模型預期無法回本"
    m = need / monthly
    return f"約 {m * 20:.0f} 個交易日" if m <= 3 else f"約 {m:.1f} 個月（超過競賽剩餘期間的風險高）"


def _swap_check(pf: Portfolio, prices: dict, code: str, alt: dict, nav: float) -> dict:
    """全部賣出 code、同日買進 alt：委託內容與競賽規則檢查（持股率、最後一日持有檔數與現金）。"""
    import math

    from src.agents import compliance
    from src.state.portfolio import trade_cost

    pos = pf.positions[code]
    px = prices[code]
    net = pos.shares * px - trade_cost(code, "SELL", pos.shares, px)
    pb = alt["price"]
    sh = int(net // pb)
    sh = (sh // 1000) * 1000 if sh >= 1000 else sh
    orders = [{"action": "SELL", "code": code, "shares": pos.shares, "limit_price": px, "amount": round(pos.shares * px),
               "holding_after": 0, "asset": "STOCK"},
              {"action": "BUY", "code": alt["code"], "shares": sh, "limit_price": pb, "amount": round(sh * pb), "asset": "STOCK"}]
    checks = compliance.check(pf, {**prices, alt["code"]: pb}, orders, now_tw().date(), nav)
    cur = pf.holding_ratio(prices)
    notes, ok = [], sh > 0
    if sh <= 0:
        notes.append(f"賣出所得約 {net:,.0f} 元，買不起 {alt['name']} 1 股")
    for c in checks:
        if c["item"].startswith("執行建議後持股率") and c["status"] == "未通過":
            after = float(c["detail"].split("預估 ")[1].split("%")[0]) / 100
            if after < cur - 0.005:
                ok = False
                notes.append(f"換股後持股率約 {after * 100:.1f}%，低於 70% 會被扣報酬率 0.2%")
        if c["item"].startswith("最後一日") and c["status"] == "未通過":
            ok = False
            notes.append(c["item"] + "：" + c["detail"])
    lots, odd = divmod(sh, 1000)
    return {"ok": ok, "notes": notes, "orders": [
        {"action": "SELL", "code": code, "shares": pos.shares, "limit_price": px, "text": f"全部賣出 {pos.shares:,} 股"},
        {"action": "BUY", "code": alt["code"], "name": alt["name"], "shares": sh, "limit_price": pb,
         "text": (f"買進 {lots} 張" + (f"又 {odd} 股" if odd else "") if lots else f"買進 {odd} 股（零股）") + f"，約 {sh * pb:,.0f} 元"}],
        "tip": "現金夠的話先買 B、成交後再賣 A，收盤持股率不會掉；兩筆都要在 13:30 前成交"}


def plan(pf: Portfolio, prices: dict, scores: dict, alpha_preds: dict, meta: dict, ranked: list[dict],
         nav: float, initial: float, today_pnl: dict[str, float], basket_of: dict | None = None,
         alts: list[dict] | None = None) -> dict:
    """alts：綜合排名（data.high_return_picks 的 items，結論為可以買、依綜合預期排序）；有傳入時用「結單轉股」規則。"""
    st = params.strategy()
    basket_of = basket_of or {}
    held = set(pf.positions)
    off_alts = [s for s in ranked if s["code"] not in held and s.get("alpha") and s.get("signal") == "買進候選"]
    def_alts = [s for s in ranked if s["code"] not in held and s.get("alpha") and s.get("def_signal") == "買進候選"]
    best = {"offense": max(off_alts, key=lambda s: s["alpha"]["exp_ret"], default=None),
            "defense": max(def_alts, key=lambda s: (s["alpha"]["p_up"], s["alpha"]["exp_ret"]), default=None)}
    prob_key = {"offense": ("p_newhigh", "創新高機率"), "defense": ("p_up", "收正報酬機率")}
    items, actions = [], {}
    alts = [x for x in (alts or []) if x["code"] not in held and x.get("verdict") == "可以買"]
    used: set[str] = set()
    # 預期最差的持股先挑替代標的（同一檔替代標的只配給一檔持股）
    order_codes = sorted(pf.positions, key=lambda c: (alpha_preds.get(c) or {}).get("exp_ret", -1))

    for code in order_codes:
        pos = pf.positions[code]
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
        held = held_days(pos, now_tw().date())
        locked = held < st.get("min_hold_days", 0)  # 剛買進：不因換股或分數小幅轉弱而賣（避免買了就賣、來回付成本）

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
        rot = next((x for x in alts if x["code"] not in used), None) if alts else None
        swap = None
        if rot and a:
            gain = rot["score"] / 100 - switch - hold_exp
            expired = held >= st.get("expiry_days", 40)
            rule = ("獲利結單" if pnl_pct >= st.get("profit_swap_min_pnl", 9) and gain > st.get("profit_swap_margin", 9) else
                    "換股" if not locked and gain > st["swap_margin"] else
                    "到期換股" if expired and gain > 0 else None)
            if rule:
                swap = {"rule": rule, "gain": gain, "to": rot, **_swap_check(pf, prices, code, rot, nav)}
            lines.append(f"結單轉股比較：綜合排名最佳未持有 {rot['code']} {rot['name']}（綜合預期 {rot['score']:+.1f}%、"
                         f"勝率 {(rot.get('p_up') or 0) * 100:.0f}%、可信度{rot.get('confidence', '—')}），扣換股成本 {switch * 100:.3f}% 後"
                         f"比續抱（{hold_exp * 100:+.1f}%）{'多' if gain >= 0 else '少'} {abs(gain) * 100:.1f}%")
        if stop and price <= stop:
            decision = "停損換股"
            why = f"收盤 {price} 已跌破停損價 {stop}，依紀律出場；資金轉往評分最高的候選標的"
        elif swap and swap["ok"]:
            if len(pf.positions) - len(actions) <= st.get("max_positions", 99):
                used.add(swap["to"]["code"])  # 已在 5 檔以內：一檔替代標的只配一檔持股；超過時讓弱勢持股併入同一檔，集中持股
            decision = "獲利換股" if pnl_pct > 0 else "換股"
            cond = {"獲利結單": f"已獲利 {pnl_pct * 100:+.1f}%（≥ {st['profit_swap_min_pnl'] * 100:.0f}%），且扣成本後多 {swap['gain'] * 100:.1f}%"
                                f"（門檻 {st['profit_swap_margin'] * 100:.0f}%），今天就可以結單",
                    "換股": f"持有 {held} 個交易日（≥ {st['min_hold_days']}），扣成本後多 {swap['gain'] * 100:.1f}%（門檻 {st['swap_margin'] * 100:.0f}%）",
                    "到期換股": f"已持有 {held} 個交易日（滿 {st['expiry_days']} 日），換股仍多 {swap['gain'] * 100:.1f}%"}[swap["rule"]]
            why = (f"結單轉股：全部賣出、同日買進 {swap['to']['code']} {swap['to']['name']}（綜合預期 {swap['to']['score']:+.1f}% vs 這檔 "
                   f"{hold_exp * 100:+.1f}%）—— {cond}；十年模擬這個規則年化中位 88%，抱滿 40 日只有 50%")
        elif not alts and not locked and pnl_pct < 0 and a and best_alt and (best_alt["alpha"]["exp_ret"] - switch - hold_exp) > st["swap_margin"] \
                and best_alt["alpha"][pk] >= hold_p:
            decision = "換股"
            gain = best_alt["alpha"]["exp_ret"] - switch - hold_exp
            why = (f"{best_alt['code']} {best_alt['name']} 扣除換股成本後的預期報酬比續抱高 {gain * 100:.2f}%"
                   f"（門檻 {st['swap_margin'] * 100:.1f}%），且{pname}不低於原標的 → 換股補回虧損較快；"
                   f"回收資金依綜合分數配置到買進候選，各檔仍受單一標的與產業上限約束")
        elif not alts and not locked and pnl_pct > 0 and a and best_alt and (best_alt["alpha"]["exp_ret"] - switch - hold_exp) > st["swap_margin"] \
                and best_alt["alpha"][pk] >= hold_p:
            # 獲利換股（2026-10-11 使用者要求）：已經賺錢、但同一籃有明顯更好的標的 → 全部賣出入袋，轉買更好的；門檻與虧損換股相同
            decision = "獲利換股"
            gain = best_alt["alpha"]["exp_ret"] - switch - hold_exp
            why = (f"已獲利 {unreal:+,.0f} 元（{pnl_pct * 100:+.2f}%）先入袋；這檔一個月預期只剩 {hold_exp * 100:+.2f}%，"
                   f"轉買 {best_alt['code']} {best_alt['name']}（預期 {best_alt['alpha']['exp_ret'] * 100:+.2f}%）扣除換股成本後多 {gain * 100:.2f}%"
                   f"（門檻 {st['swap_margin'] * 100:.1f}%），且{pname}不低於原標的")
        elif not locked and (s.get("def_score", 0) if basket == "defense" else s.get("score", 0)) <= st["sell_score_threshold"]:
            decision = "減碼"
            why = "分數已低於賣出門檻，先減碼一半降低風險，資金轉往同一籃的高分標的"
        elif a and hold_exp > 0 and a["def_combo" if basket == "defense" else "combo"] >= 0.8 and pnl_pct > -0.05 and mv / nav < st["max_weight"] * 0.8 \
                and (s.get("features") or {}).get("close", 0) > (s.get("features") or {}).get("ma20", 1e18):
            decision = "續抱並可加碼"
            why = (f"模型排名仍在全市場前 {(1 - a['def_combo' if basket == 'defense' else 'combo']) * 100:.0f}%、股價在月線之上、虧損未擴大，"
                   f"權重 {mv / nav * 100:.1f}% 仍低於上限，可逢回加碼攤低成本")
        elif swap and not swap["ok"]:
            why = (f"有結單轉股機會（{swap['to']['code']} {swap['to']['name']} 扣成本後多 {swap['gain'] * 100:.1f}%），"
                   f"但{'；'.join(swap['notes'])}，先續抱")
        elif locked:
            why = (f"買進第 {held} 個交易日，未滿 {st['min_hold_days']} 個交易日不換股、不因分數小幅轉弱賣出"
                   f"（模型排名每天都會小幅變動，來回換股要付約 {switch * 100:.2f}% 成本）；跌破停損 {stop or '—'} 才出場")
            if alts:
                if rot and a and (rot["score"] / 100 - switch - hold_exp) > st["swap_margin"]:
                    why += (f"。提醒：綜合排名 {rot['code']} {rot['name']} 扣成本後多 {(rot['score'] / 100 - switch - hold_exp) * 100:.1f}%，"
                            f"滿 {st['min_hold_days']} 個交易日（或獲利達 {st['profit_swap_min_pnl'] * 100:.0f}%）後若仍如此會建議結單轉股")
            elif pnl_pct > 0 and a and best_alt and (best_alt["alpha"]["exp_ret"] - switch - hold_exp) > st["swap_margin"]:
                why += (f"。提醒：已獲利 {pnl_pct * 100:+.2f}%，{best_alt['code']} {best_alt['name']} 預期報酬較高，"
                        f"滿 {st['min_hold_days']} 個交易日後若仍如此會建議獲利換股")
        else:
            why = ("模型預期仍為正，未觸及停損，換股的預期效益不足以支付換股成本" if (a and hold_exp > 0)
                   else "未觸及停損，但模型預期偏弱，密切觀察；若跌破停損即出場")
        lines.append(f"決策：{decision} —— {why}")
        alt_used = swap["to"] if swap and swap["ok"] and decision in ("換股", "獲利換股") else best_alt
        swap_out = ({"to": {k: swap["to"].get(k) for k in ("code", "name", "score", "p_up", "confidence", "price", "exit")},
                     "gain_pct": round(swap["gain"] * 100, 2), "rule": swap["rule"], "orders": swap["orders"],
                     "compliance_ok": swap["ok"], "compliance_notes": swap["notes"], "tip": swap["tip"]} if swap else None)
        items.append({"code": code, "name": s.get("name", code), "basket": basket,
                      "unrealized": round(unreal), "pnl_pct": round(pnl_pct * 100, 2), "held_days": held,
                      "decision": decision, "why": why, "lines": lines, "exp_ret": hold_exp, "p_newhigh": hold_p,
                      "swap": swap_out,
                      "alt": {"code": alt_used["code"], "name": alt_used["name"]} if alt_used and decision in ("換股", "停損換股", "獲利換股") else None})
        if decision in ("換股", "停損換股", "獲利換股", "減碼"):
            actions[code] = {"decision": decision, "why": why, "swap": swap_out if decision in ("換股", "獲利換股") else None,
                             "alt": f"{alt_used['code']} {alt_used['name']}" if alt_used else None}

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
