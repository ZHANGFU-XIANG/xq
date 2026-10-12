"""HoldingAdviceAgent：庫存中每一檔股票給出單一結論 —— 加碼／續抱／減碼／全部出清，附數量、限價與理由。

整合來源（優先順序由高到低）：
1. 賣出訂單（停損、分段停利、虧損補回換股／減碼、訊號轉弱、權重超標）
2. 風險警示的建議賣出股數（跌停、負面新聞、處置股、單日重挫）—— 若比賣出訂單更多，以警示為準
3. 同一檔的加碼買進訂單（模型分數仍達門檻、權重未滿）
4. 以上皆無 → 續抱，並列出「什麼情況下要動作」的條件（停損價、下一段停利價）
所有數字由程式計算。
"""
from __future__ import annotations

from src.agents import sizer
from src.config import stock_meta
from src.state import params
from src.state.portfolio import Portfolio, is_etf, trade_cost


def merge_alert_sells(orders: list[dict], alert_items: list[dict], pf: Portfolio, prices: dict,
                      basket_of: dict) -> list[dict]:
    """把風險警示建議的賣出併入訂單：警示賣得比訂單多時以警示為準；同時取消該檔的加碼買單。"""
    out = list(orders)
    for a in alert_items:
        if a.get("scope") != "持股" or not a.get("sell_shares") or a["code"] not in pf.positions:
            continue
        code, qty = a["code"], int(a["sell_shares"])
        held = pf.positions[code].shares
        existing = next((o for o in out if o["code"] == code and o["action"] == "SELL"), None)
        alert_why = "風險警示：" + "；".join(a["reasons"])
        if existing and existing["shares"] >= qty:
            # 原本的賣單已經賣得比警示多：數量照原賣單，但理由要把警示也寫上（警示頁、持股建議、個股頁說法一致）
            if alert_why not in existing["why_sell"]:
                existing["why_sell"] += f"；同時{alert_why}"
            continue
        price = a.get("price") or prices.get(code) or pf.positions[code].avg_cost
        lots, odd = sizer.split_lots(qty, True)
        order = {
            "action": "SELL", "code": code, "name": stock_meta(code)["name"], "basket": basket_of.get(code, "offense"),
            "shares": qty, "lots": lots, "odd": odd,
            "limit_price": a.get("limit_price") or sizer.round_tick(price * 0.99, is_etf(code)),
            "ref_price": price, "amount": round(qty * price), "est_cost": trade_cost(code, "SELL", qty, price),
            "why_sell": alert_why + (f"；另外{existing['why_sell']}" if existing else ""),
            "why_qty": a.get("why_qty") or f"依警示規則賣出 {qty / held * 100:.0f}%",
            "pnl_pct": round((price / pf.positions[code].avg_cost - 1) * 100, 2) if pf.positions[code].avg_cost else 0,
            "holding_after": held - qty, "from_alert": True,
        }
        out = [o for o in out if not (o["code"] == code and (o["action"] == "SELL" or o["action"] == "BUY"))]
        out.insert(0, order)
    return out


def build(pf: Portfolio, prices: dict, scores: dict, alpha_preds: dict, meta: dict, orders: list[dict],
          alert_items: list[dict], basket_of: dict, nav: float) -> list[dict]:
    items = []
    alerts_by = {a["code"]: a for a in alert_items if a.get("scope") == "持股"}
    for code, pos in pf.positions.items():
        price = prices.get(code) or pos.avg_cost
        basket = basket_of.get(code, "offense")
        bcfg = params.basket(basket)
        m = meta.get(code, {})
        s = scores.get(code, {})
        a = alpha_preds.get(code) or s.get("alpha") or {}
        stop = m.get("stop")
        mv = pos.shares * price
        pnl = mv - pos.cost_basis
        sell = next((o for o in orders if o["code"] == code and o["action"] == "SELL"), None)
        buy = next((o for o in orders if o["code"] == code and o["action"] == "BUY"), None)
        alert = alerts_by.get(code)
        reasons, conditions = [], []
        score = s.get("def_score") if basket == "defense" else s.get("score")
        label = "穩健分數" if basket == "defense" else "綜合分數"

        if sell:
            qty = sell["shares"]
            verdict = "全部出清" if qty >= pos.shares else "減碼"
            qty_text = (f"賣出全部 {sizer.fmt_shares(qty)}" if verdict == "全部出清"
                        else f"賣出 {sizer.fmt_shares(qty)}，保留 {sizer.fmt_shares(pos.shares - qty)}")
            limit = sell["limit_price"]
            reasons += [sell["why_sell"], sell["why_qty"]]
        elif buy:
            qty, verdict, limit = buy["shares"], "加碼", buy["limit_price"]
            qty_text = f"加買 {sizer.fmt_shares(qty)}，加碼後共 {sizer.fmt_shares(pos.shares + qty)}"
            reasons += buy["why_stock"][:3] + [x for x in buy["why_qty"] if "取整後" in x or "風險預算" in x]
            if buy.get("stop"):
                conditions.append(f"加碼部位停損 {buy['stop']}")
        else:
            qty, verdict, limit = 0, "續抱", None
            qty_text = f"維持 {sizer.fmt_shares(pos.shares)}"
            if score is not None:
                reasons.append(f"{label} {score:+.2f}，未觸發賣出條件（≤ {params.strategy()['sell_score_threshold']:+.2f}）"
                               + ("，也未達加碼條件（權重已足或分數未達門檻）" if score < bcfg["buy_threshold"]
                                  or mv / nav >= bcfg["max_weight"] * 0.6 else ""))
            if a:
                key = ("p_up", "收正報酬機率") if basket == "defense" else ("p_newhigh", "創新高機率")
                reasons.append(f"一個月模型：預期報酬 {a['exp_ret'] * 100:+.2f}%、{key[1]} {a[key[0]] * 100:.1f}%")
            if alert:
                reasons += [f"注意：{r}" for r in alert["reasons"]]
                if alert.get("new_stop"):
                    conditions.append(f"建議停損上調至 {alert['new_stop']}")

        # 條件式出場計畫（所有結論都附上）
        if stop:
            dist = (price / stop - 1) * 100
            conditions.append(f"收盤跌破停損 {stop}（距今 {dist:.1f}%）→ 全部出清")
        tp = bcfg["take_profit"]
        stage = m.get("tp_stage", 0)
        if stage < len(tp) and verdict != "全部出清":
            lvl, frac = tp[stage]
            if verdict == "加碼":  # 加碼後平均成本改變，停利價以新成本計算
                remain = pos.shares + qty
                cost = (pos.cost_basis + qty * limit) / remain
                basis_txt = f"加碼後成本約 {cost:.2f}"
            else:
                remain = pos.shares - (qty if verdict == "減碼" else 0)
                cost = pos.avg_cost
                basis_txt = "成本"
            tp_price = sizer.round_tick(cost * (1 + lvl), is_etf(code))
            tp_qty = sizer._round_sell(remain * frac, remain, params.strategy()["allow_odd_lot"])
            conditions.append(f"漲到 {tp_price}（{basis_txt} +{lvl * 100:.0f}%）→ 第 {stage + 1} 段停利，賣出 {sizer.fmt_shares(tp_qty)}")
        if a and a.get("hi252") and basket == "offense" and verdict in ("續抱", "加碼"):
            conditions.append(f"突破 52 週高點 {a['hi252']} → 確認創新高，續抱讓獲利奔跑")

        items.append({
            "code": code, "name": stock_meta(code)["name"], "basket": basket, "basket_name": bcfg["name"],
            "shares": pos.shares, "avg_cost": round(pos.avg_cost, 2), "price": round(price, 2),
            "pnl": round(pnl), "pnl_pct": round(pnl / pos.cost_basis * 100, 2) if pos.cost_basis else 0,
            "weight_pct": round(mv / nav * 100, 2) if nav else 0, "stop": stop,
            "swap": (sell or {}).get("swap"),
            "verdict": verdict, "qty": qty, "qty_text": qty_text, "limit_price": limit,
            "reasons": [r for r in reasons if r], "conditions": conditions, "score": score,
            "model": {k: a.get(k) for k in ("exp_ret", "p_newhigh", "p_up", "exp_dd")} if a else None,
        })
    order = {"全部出清": 0, "減碼": 1, "加碼": 2, "續抱": 3}
    return sorted(items, key=lambda x: (order[x["verdict"]], -x["weight_pct"]))
