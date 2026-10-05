"""PositionSizerAgent：依「進攻籃 / 穩健籃 / ETF 籃」配置比例決定買賣股數／張數，並把每一步計算寫成理由。

買進（每一籃各自）：籃子目標金額 → 風險預算法 → 各項上限約束 → 補足籃子目標 → 資金分配 → 取整為張與零股
- 穩健籃與 ETF 籃先配置、只用現金（不融資）、限價不高於前一日收盤（不追價）；ETF 同類別最多一檔
- 進攻籃後配置、可融資；RSI 過高時限價等回檔
賣出：停損、分批停利（依籃子）、虧損補回計畫（換股/減碼）、訊號轉弱、權重超標
所有數字都由本模組計算；報告只引用這裡產生的文字與數值。
"""
from __future__ import annotations

import math

from src.config import now_tw, rules, stock_meta
from src.state import params
from src.state.portfolio import Portfolio, held_days, is_etf, trade_cost

LOT = 1000
BASKETS = ("defense", "etf", "offense")  # 配置順序：穩健籃、ETF 籃（只用現金）→ 進攻籃（可融資）


def tick_size(price: float, etf: bool = False) -> float:
    if etf:
        return 0.01 if price < 50 else 0.05
    for limit, tick in ((10, 0.01), (50, 0.05), (100, 0.1), (500, 0.5), (1000, 1.0)):
        if price < limit:
            return tick
    return 5.0


def round_tick(price: float, etf: bool = False, up: bool = False) -> float:
    t = tick_size(price, etf)
    n = math.ceil(price / t - 1e-9) if up else math.floor(price / t + 1e-9)
    return round(n * t, 2)


def split_lots(shares: int, allow_odd: bool) -> tuple[int, int]:
    lots, odd = divmod(max(0, int(shares)), LOT)
    return lots, (odd if allow_odd else 0)


def fmt_shares(shares: int) -> str:
    lots, odd = divmod(int(shares), LOT)
    if lots and odd:
        return f"{lots} 張又 {odd} 股（共 {shares:,} 股）"
    if lots:
        return f"{lots} 張（{shares:,} 股）"
    return f"{odd} 股（零股）"


def round_trip_cost_pct(code: str) -> float:
    c = rules()["costs"]
    return (2 * c["commission"] + (c["etf_tax"] if is_etf(code) else c["stock_tax"])) * 100


def basket_score(s: dict, basket: str) -> float:
    if basket == "etf":
        return s.get("etf_score", 0.0)
    return s.get("def_score", 0.0) if basket == "defense" else s.get("score", 0.0)


# ======================================================================= 賣出
def plan_sells(pf: Portfolio, prices: dict, feats: dict, scores: dict, meta: dict, nav: float,
               recovery_actions: dict | None = None) -> list[dict]:
    recovery_actions = recovery_actions or {}
    st = params.strategy()
    allow_odd = st["allow_odd_lot"]
    orders = []
    for code, pos in pf.positions.items():
        price = prices.get(code)
        f = feats.get(code, {})
        if not price or not f:
            continue
        m = meta.get(code, {})
        basket = m.get("basket", "offense")
        bcfg = params.basket(basket)
        pnl = price / pos.avg_cost - 1 if pos.avg_cost else 0.0
        stop = m.get("stop", pos.avg_cost * (1 - st["max_stop_pct"]))
        stage = m.get("tp_stage", 0)
        s = scores.get(code, {})
        score = basket_score(s, basket)
        label = {"defense": "穩健分數", "etf": "ETF 分數"}.get(basket, "綜合分數")
        weight = pos.shares * price / nav if nav else 0
        tp = bcfg["take_profit"]
        sell, why, why_qty = 0, "", ""
        locked = held_days(pos, now_tw().date()) < st.get("min_hold_days", 0)  # 剛買進：分數小幅轉弱、權重超標不賣

        if price <= stop:
            sell = pos.shares
            why = f"觸發停損：收盤 {price:.2f} 元 ≤ 停損價 {stop:.2f} 元（成本 {pos.avg_cost:.2f} 元，損益 {pnl * 100:+.2f}%）"
            why_qty = "停損一律全數出場，避免虧損擴大；紀律優先於預測"
        elif stage < len(tp) and pnl >= tp[stage][0]:
            lvl, frac = tp[stage]
            raw = pos.shares * frac
            sell = _round_sell(raw, pos.shares, allow_odd)
            why = f"{bcfg['name']}第 {stage + 1} 段停利：報酬 {pnl * 100:+.2f}% ≥ {lvl * 100:.0f}%"
            why_qty = (f"持有 {pos.shares:,} 股 × {frac * 100:.0f}% = {raw:,.0f} 股，取整為 {fmt_shares(sell)}；"
                       f"先實現部分獲利、保留其餘部位參與後續漲幅，停損上移至成本價保本")
        elif recovery_actions.get(code, {}).get("decision") in ("換股", "停損換股"):
            ra = recovery_actions[code]
            sell = pos.shares
            why = f"虧損補回計畫 → {ra['decision']}：{ra['why']}"
            why_qty = (f"全數賣出 {fmt_shares(sell)}，回收資金約 {sell * price:,.0f} 元，依分數重新配置到同一籃的買進候選"
                       + (f"（最佳替換標的 {ra['alt']}，實際買進股數受單一標的與產業上限約束，見買進明細）" if ra.get("alt") else "")
                       + "；部分換股會讓兩檔都表現平庸，因此一次換完")
        elif recovery_actions.get(code, {}).get("decision") == "減碼":
            ra = recovery_actions[code]
            raw = pos.shares * 0.5
            sell = _round_sell(raw, pos.shares, allow_odd)
            why = f"虧損補回計畫 → 減碼：{ra['why']}"
            why_qty = f"先賣一半 {raw:,.0f} 股（取整為 {fmt_shares(sell)}），保留一半避免賣在低點，若續弱再出清"
        elif score <= -0.6:
            sell = pos.shares
            why = f"訊號全面轉空：{label} {score:+.2f} ≤ -0.60"
            why_qty = "多項因子同時轉空，全數出場並將資金轉往高分標的"
        elif not locked and score <= st["sell_score_threshold"]:
            raw = pos.shares * 0.5
            sell = _round_sell(raw, pos.shares, allow_odd)
            why = f"訊號轉弱：{label} {score:+.2f} ≤ {st['sell_score_threshold']:+.2f}"
            why_qty = f"先減碼一半 {raw:,.0f} 股（取整為 {fmt_shares(sell)}），保留部位避免誤判，若續弱再出清"
        elif not locked and weight > bcfg["max_weight"] + 0.05:
            target = bcfg["max_weight"] * nav / price
            raw = pos.shares - target
            sell = _round_sell(raw, pos.shares, allow_odd)
            why = f"權重超標：目前佔總資產 {weight * 100:.1f}% > {bcfg['name']}單檔上限 {bcfg['max_weight'] * 100:.0f}%"
            why_qty = f"減碼至上限需賣 {raw:,.0f} 股，取整為 {fmt_shares(sell)}，控制單一標的風險"
        if sell > 0:
            lots, odd = split_lots(sell, True)
            orders.append({
                "action": "SELL", "code": code, "name": s.get("name", code), "basket": basket,
                "shares": int(sell), "lots": lots, "odd": odd,
                "limit_price": round_tick(price * 0.99, is_etf(code)),
                "ref_price": price, "amount": round(sell * price),
                "est_cost": trade_cost(code, "SELL", int(sell), price),
                "why_sell": why, "why_qty": why_qty, "pnl_pct": round(pnl * 100, 2),
                "holding_after": pos.shares - int(sell),
            })
    return orders


def _round_sell(raw: float, held: int, allow_odd: bool) -> int:
    if raw >= held:
        return held
    if allow_odd:
        return max(1, int(raw))
    return min(held, max(LOT, int(raw // LOT) * LOT))


# ======================================================================= 買進
def _pick_reason(s: dict, basket: str) -> str:
    if basket == "etf":
        return (f"【ETF 籃】{s.get('etf_category', '')}型 ETF，ETF 分數 {s.get('etf_score', 0):+.2f} 達門檻"
                f"（{params.basket('etf')['buy_threshold']:+.2f}）；分散個股風險、證交稅 0.1%")
    bcfg = params.basket(basket)
    a = s.get("alpha") or {}
    if basket == "defense":
        r = f"【{bcfg['name']}】穩健分數 {s['def_score']:+.2f} 達門檻（{bcfg['buy_threshold']:+.2f}）"
        if a:
            r += f"；一個月收正報酬機率 {a['p_up'] * 100:.1f}%、預期最大回檔 {a['exp_dd'] * 100:.1f}%，且未過熱"
        return r
    r = f"【{bcfg['name']}】綜合分數 {s['score']:+.2f} 達門檻（{bcfg['buy_threshold']:+.2f}）"
    if a:
        r += f"；一個月模型預期超額報酬排名全市場前 {(1 - a['alpha_rank']) * 100:.0f}%、創新高機率 {a['p_newhigh'] * 100:.1f}%"
    return r


def select_candidates(ranked: list[dict], held_after: dict, basket_of: dict, prices: dict, nav: float) -> dict:
    """兩籃候選；同一檔只會進一籃（進攻優先，穩健籃排除進攻候選）。"""
    st = params.strategy()
    out = {}
    taken: set[str] = set()
    for basket in ("offense", "defense", "etf"):
        bcfg = params.basket(basket)
        held_in = [c for c in held_after if basket_of.get(c, "offense") == basket]
        slots = bcfg["max_positions"] - len(held_in)
        key = {"offense": "signal", "defense": "def_signal", "etf": "etf_signal"}[basket]
        cands = []
        cats: dict[str, int] = {}
        for c in held_in:
            cat = (next((x for x in ranked if x["code"] == c), {}) or {}).get("etf_category")
            if cat:
                cats[cat] = cats.get(cat, 0) + 1
        for s in sorted(ranked, key=lambda x: -basket_score(x, basket)):
            if s.get(key) != "買進候選" or not s["features"] or s["code"] in taken:
                continue
            if (basket == "etf") != bool(stock_meta(s["code"]).get("is_etf")):  # 股票籃不放 ETF、ETF 籃只放 ETF
                continue
            if basket == "etf" and s["code"] not in held_after:
                cat = s.get("etf_category", "其他")
                if cats.get(cat, 0) >= bcfg["max_per_category"]:
                    continue
                cats[cat] = cats.get(cat, 0) + 1
            if s["code"] in held_after and basket_of.get(s["code"], "offense") != basket:
                continue
            w = held_after.get(s["code"], 0) * prices.get(s["code"], 0) / nav
            if s["code"] in held_after and w >= bcfg["max_weight"] * 0.6:
                continue
            if s["code"] not in held_after:
                if slots <= 0:
                    continue
                slots -= 1
            cands.append({**s, "basket": basket, "pick_reason": _pick_reason(s, basket)})
            taken.add(s["code"])
        out[basket] = cands
    # 規則：至少 5 個標的 → 補位（ETF 歸 ETF 籃、股票歸穩健籃）
    n = len(set(held_after) | taken)
    if n < st["min_positions"]:
        pool = sorted(ranked, key=lambda x: (x["code"] not in st["etf_fillers"], -x.get("def_score", -9)))
        for s in pool:
            if n >= st["min_positions"]:
                break
            if s["code"] in held_after or s["code"] in taken or not s["features"]:
                continue
            if s.get("def_score", -1) > 0 or s["code"] in st["etf_fillers"]:
                b = "etf" if stock_meta(s["code"]).get("is_etf") else "defense"
                out[b].append({**s, "basket": b, "pick_reason":
                                       f"【穩健籃】補足競賽「至少 {st['min_positions']} 個標的」規則"
                                       f"（穩健分數 {s.get('def_score', 0):+.2f}）"})
                taken.add(s["code"])
                n += 1
    return out


def _entry(c: dict, price: float, f: dict, basket: str, etf: bool) -> tuple[float, str]:
    st = params.strategy()
    pred = c.get("prediction") or {}
    pred_open = pred.get("pred_open", price)
    if basket in ("defense", "etf"):
        entry = round_tick(min(pred_open, price), etf)
        return entry, f"穩健籃不追價：限價取預測開盤價 {pred_open} 與前一日收盤 {price} 的較低者 = {entry}"
    if pred.get("intra_reliable") and pred.get("intraday_pct", 0) < 0:
        entry = round_tick(pred_open * (1 + pred["intraday_pct"] / 100), etf)
        note = f"模型預測開高回落，限價設於預測開盤價 {pred_open} × (1 {pred['intraday_pct']:+.2f}%) = {entry}，不追高"
    else:
        entry = round_tick(pred_open * (1 + st["limit_buffer"]), etf, up=True)
        note = f"限價 = 預測開盤價 {pred_open} × (1 + {st['limit_buffer'] * 100:.1f}%) = {entry}，提高成交機率"
    rsi = ((c.get("alpha") or {}).get("features") or {}).get("rsi14")
    ac = params.basket("offense")["anti_chase"]
    if rsi and rsi > ac["pullback_rsi"]:
        pull = round_tick(price - 0.5 * f["atr14"], etf)
        if pull < entry:
            entry = pull
            note = (f"RSI14 = {rsi:.1f} > {ac['pullback_rsi']} 偏熱，不追價：限價設在前一日收盤 {price} − 0.5×ATR "
                    f"{f['atr14']:.2f} = {entry}，等回檔才買（可能當日不成交）")
    return min(entry, round_tick(price * 1.095, etf)), note


def _plan_one(c: dict, basket: str, f: dict, price: float, nav: float, held_after: dict, sector_value: dict,
              target_val: float, held_val: float) -> dict:
    """單檔：進場價、停損距離、風險預算股數與各項上限（每日建議與 AI 助理試算共用）。"""
    st = params.strategy()
    bcfg = params.basket(basket)
    etf = is_etf(c["code"])
    entry, entry_note = _entry(c, price, f, basket, etf)
    atr_dist = st["atr_mult_stop"] * f["atr14"]
    low_dist = entry - f["low10"] if f["low10"] < entry else 0.0
    stop_dist = min(max(atr_dist, low_dist), entry * st["max_stop_pct"])
    stop_dist = max(stop_dist, f["atr14"])
    stop_note = (f"取 {st['atr_mult_stop']:g}×ATR14 = {atr_dist:.2f} 與距 10 日低點 {f['low10']:.2f} 的距離 {low_dist:.2f} 之較大者"
                 + (f"，但不超過進場價 {st['max_stop_pct'] * 100:.0f}%（{entry * st['max_stop_pct']:.2f}）"
                    if max(atr_dist, low_dist) > entry * st["max_stop_pct"] else "")
                 + f" = {stop_dist:.2f} 元")
    sc = basket_score(c, basket)
    conf = float(min(1.5, max(0.5, 0.5 + 1.25 * sc)))
    budget = nav * st["risk_per_trade"] * conf
    raw = budget / stop_dist
    existing = held_after.get(c["code"], 0) * price
    caps = {
        f"{bcfg['name']}單檔上限 {bcfg['max_weight'] * 100:.0f}%": max(0.0, (bcfg["max_weight"] * nav - existing) / entry),
        f"流動性上限（20 日均量 {f['avg_vol20'] / 1000:,.0f} 張 × {st['liquidity_cap'] * 100:.0f}%）": st["liquidity_cap"] * f["avg_vol20"],
        f"產業上限 {st['sector_max_weight'] * 100:.0f}%（{c['sector']}）":
            max(0.0, (st["sector_max_weight"] * nav - sector_value.get(c["sector"], 0)) / entry) if c["sector"] != "ETF" else math.inf,
    }
    cap_name, cap_val = min(caps.items(), key=lambda kv: kv[1])
    return {"c": c, "basket": basket, "entry": entry, "entry_note": entry_note, "stop_note": stop_note,
            "stop_dist": stop_dist, "conf": conf, "budget": budget, "raw": raw, "score": sc,
            "cap_name": cap_name, "cap_val": cap_val, "desired": min(raw, cap_val), "etf": etf,
            "basket_target": target_val, "basket_held": held_val}


def size_single(s: dict, basket: str, pf: Portfolio, prices: dict, feats: dict, nav: float, alloc: dict,
                basket_of: dict) -> dict:
    """AI 助理用：若現在買這一檔（指定籃子），依目前持股、現金與籃子剩餘額度要買幾股、限價、停損與出場計畫。"""
    st = params.strategy()
    credit = rules()["credit"]
    bcfg = params.basket(basket)
    held_after = {c: p.shares for c, p in pf.positions.items()}
    sector_value: dict[str, float] = {}
    for c, sh in held_after.items():
        sec = stock_meta(c)["sector"]
        sector_value[sec] = sector_value.get(sec, 0) + sh * prices.get(c, 0)
    held_val = sum(sh * prices.get(c, 0) for c, sh in held_after.items() if basket_of.get(c, "offense") == basket)
    target_val = alloc[basket] * st["target_holding_ratio"] * nav
    c = {**s, "basket": basket, "pick_reason": _pick_reason(s, basket) if basket_score(s, basket) >= bcfg["buy_threshold"]
         else f"【{bcfg['name']}】使用者詢問的試算（分數 {basket_score(s, basket):+.2f} 未達門檻 {bcfg['buy_threshold']:+.2f}，僅供參考）"}
    p = _plan_one(c, basket, feats[s["code"]], prices[s["code"]], nav, held_after, sector_value, target_val, held_val)
    room = max(0.0, alloc[basket] * st["max_holding_ratio"] * nav - held_val)
    notes = []
    if p["desired"] * p["entry"] > room:
        p["scaled_down"] = p["desired"] - room / p["entry"]
        p["desired"] = room / p["entry"]
        notes.append(f"{bcfg['name']}剩餘額度 {room:,.0f} 元，股數受此限制")
    transfer = max(0.0, pf.futures_cash - st["futures_reserve"])
    cash = pf.cash + transfer
    loan_left = max(0.0, st["max_margin_loan_ratio"] * nav - pf.margin_loan())
    want = p["desired"] * p["entry"]
    margin = False
    if want > cash:
        can_margin = bcfg["allow_margin"] and st["allow_margin"] and loan_left > 0
        mroom = min(loan_left / credit["margin_ratio"], cash / (1 - credit["margin_ratio"])) if can_margin else 0
        margin = mroom > cash
        want = min(want, mroom) if margin else cash
    lots, odd = split_lots(int(max(0.0, want) / p["entry"]), st["allow_odd_lot"])
    shares = lots * LOT + odd
    if shares <= 0:
        return {"shares": 0, "notes": notes + ["目前可用資金或籃子額度不足，無法買進"], "basket": basket}
    amount = shares * p["entry"]
    order = _buy_order(p, shares, lots, odd, amount, trade_cost(s["code"], "BUY", shares, p["entry"]), margin, nav, alloc)
    order["notes"] = notes
    return order


def plan_buys(ranked: list[dict], pf: Portfolio, prices: dict, nav: float, sells: list[dict], feats: dict,
              alloc: dict, basket_of: dict, futures_reserve: float | None = None) -> dict:
    st = params.strategy()
    credit = rules()["credit"]
    allow_odd = st["allow_odd_lot"]

    sold = {o["code"]: o["shares"] for o in sells}
    held_after = {c: p.shares - sold.get(c, 0) for c, p in pf.positions.items() if p.shares - sold.get(c, 0) > 0}
    sector_value: dict[str, float] = {}
    by_code = {s["code"]: s for s in ranked}
    for c, sh in held_after.items():
        sec = by_code.get(c, {}).get("sector", "其他")
        sector_value[sec] = sector_value.get(sec, 0) + sh * prices.get(c, 0)

    # 資金：現金 + 賣出回收 + 期貨戶可轉入金額 + 融資額度
    sell_proceeds = sum(o["amount"] - o["est_cost"] for o in sells)
    reserve = st["futures_reserve"] if futures_reserve is None else futures_reserve  # 期貨戶需留存（保證金×緩衝）
    transfer = max(0.0, pf.futures_cash - reserve)
    cash_left = pf.cash + sell_proceeds + transfer
    loan_left = max(0.0, st["max_margin_loan_ratio"] * nav - pf.margin_loan()) if st["allow_margin"] else 0.0

    cands = select_candidates(ranked, held_after, basket_of, prices, nav)
    orders, summary = [], {}
    for basket in BASKETS:  # 穩健籃先配置（只用現金），進攻籃後配置（可融資）
        bcfg = params.basket(basket)
        held_val = sum(sh * prices.get(c, 0) for c, sh in held_after.items() if basket_of.get(c, "offense") == basket)
        target_val = alloc[basket] * st["target_holding_ratio"] * nav
        max_val = alloc[basket] * st["max_holding_ratio"] * nav
        plans = []
        for c in cands[basket]:
            pl = _plan_one(c, basket, feats[c["code"]], prices[c["code"]], nav, held_after, sector_value,
                           target_val, held_val)
            plans.append(pl)
            sector_value[c["sector"]] = sector_value.get(c["sector"], 0) + pl["desired"] * pl["entry"]

        # 補足籃子目標（不超過各自上限）
        for _ in range(3):
            gap = target_val - held_val - sum(p["desired"] * p["entry"] for p in plans)
            room = [p for p in plans if p["cap_val"] - p["desired"] > 1]
            if gap <= nav * 0.01 or not room:
                break
            room_value = sum((p["cap_val"] - p["desired"]) * p["entry"] for p in room)
            ratio = min(1.0, gap / room_value)
            for p in room:
                add = (p["cap_val"] - p["desired"]) * ratio
                p["desired"] += add
                p["topped_up"] = p.get("topped_up", 0) + add
        # 超過籃子上限 → 等比例縮減
        new_val = sum(p["desired"] * p["entry"] for p in plans)
        if plans and held_val + new_val > max_val:
            ratio = max(0.0, (max_val - held_val) / new_val)
            for p in plans:
                p["scaled_down"] = p["desired"] * (1 - ratio)
                p["desired"] *= ratio

        # 資金分配（分數高者優先）
        plans.sort(key=lambda p: -p["score"])
        spent = 0.0
        for p in plans:
            code = p["c"]["code"]
            want = p["desired"] * p["entry"]
            use_margin = False
            if want > cash_left:
                can_margin = bcfg["allow_margin"] and st["allow_margin"] and loan_left > 0
                margin_room = min(loan_left / credit["margin_ratio"], cash_left / (1 - credit["margin_ratio"])) if can_margin else 0
                if margin_room > cash_left:
                    use_margin = True
                    want = min(want, margin_room)
                else:
                    want = cash_left
            shares = int(max(0.0, want) / p["entry"])
            lots, odd = split_lots(shares, allow_odd)
            shares = lots * LOT + odd
            if shares <= 0 or shares * p["entry"] < st["min_order_pct"] * nav:  # 太小的單只增加成本與交易筆數
                continue
            amount = shares * p["entry"]
            cost = trade_cost(code, "BUY", shares, p["entry"])
            if use_margin:
                loan = amount * credit["margin_ratio"]
                cash_left -= amount - loan + cost
                loan_left -= loan
            else:
                cash_left -= amount + cost
            spent += amount
            orders.append(_buy_order(p, shares, lots, odd, amount, cost, use_margin, nav, alloc))
        summary[basket] = {"target": round(target_val), "held": round(held_val), "new": round(spent),
                           "after_pct": round((held_val + spent) / nav * 100, 2) if nav else 0}

    for o in orders:
        o.setdefault("asset", "ETF" if is_etf(o["code"]) else "STOCK")
    return {"orders": orders, "transfer_from_futures": round(transfer), "futures_reserve": round(reserve),
            "cash_after": round(cash_left),
            "loan_capacity_left": round(loan_left), "baskets": summary}


def _buy_order(p: dict, shares: int, lots: int, odd: int, amount: float, cost: float, margin: bool,
               nav: float, alloc: dict) -> dict:
    st = params.strategy()
    basket = p["basket"]
    bcfg = params.basket(basket)
    c = p["c"]
    code = c["code"]
    label = {"defense": "穩健分數", "etf": "ETF 分數"}.get(basket, "綜合分數")
    entry, stop = p["entry"], round_tick(p["entry"] - p["stop_dist"], p["etf"])
    raw_shares = p["raw"]
    why_qty = [
        f"籃子配置：{bcfg['name']}佔目標持股 {alloc[basket] * 100:.0f}% → 目標金額 {p['basket_target']:,.0f} 元"
        f"（已持有 {p['basket_held']:,.0f} 元）",
        f"風險預算：總資產 {nav:,.0f} 元 × 每筆風險 {st['risk_per_trade'] * 100:.1f}% × 信心係數 {p['conf']:.2f}"
        f"（由{label} {p['score']:+.2f} 換算）= {p['budget']:,.0f} 元",
        f"停損距離：{p['stop_note']}（進場 {entry:.2f} → 停損 {stop:.2f} 元，{p['stop_dist'] / entry * 100:.1f}%）",
        f"風險法可買股數 = {p['budget']:,.0f} ÷ {p['stop_dist']:.2f} = {raw_shares:,.0f} 股",
    ]
    if p["cap_val"] < raw_shares:
        why_qty.append(f"受「{p['cap_name']}」限制，下修至 {p['cap_val']:,.0f} 股")
    if p.get("scaled_down", 0) > 1:
        why_qty.append(f"{bcfg['name']}合計超過籃子上限，等比例下修 {p['scaled_down']:,.0f} 股")
    if p.get("topped_up", 0) > 1:
        why_qty.append(f"為補足{bcfg['name']}目標金額上調 {p['topped_up']:,.0f} 股")
    if shares < p["desired"] - 1:
        why_qty.append(f"受可用資金限制，實際買 {shares:,} 股")
    why_qty.append("進場價：" + p["entry_note"])
    why_qty.append(f"取整後：{fmt_shares(shares)}，金額約 {amount:,.0f} 元，佔總資產 {amount / nav * 100:.1f}%")

    more, less = [], []
    if p["conf"] >= 1.0:
        more.append(f"{label} {p['score']:+.2f} 高，信心係數 {p['conf']:.2f} ≥ 1 放大部位")
    else:
        less.append(f"{label} {p['score']:+.2f} 普通，信心係數 {p['conf']:.2f} < 1 縮小部位")
    atr_pct = c["features"]["atr14"] / c["features"]["close"]
    (less if atr_pct > 0.035 else more).append(
        f"日均波動（ATR）{atr_pct * 100:.1f}%，{'波動大 → 停損距離遠 → 股數少' if atr_pct > 0.035 else '波動溫和 → 可承擔較多股數'}")
    if c["features"]["inst_buy_streak"] >= 3:
        more.append(f"法人連續買超 {c['features']['inst_buy_streak']} 日，籌碼穩定")
    if "流動性" in p["cap_name"] and p["cap_val"] < raw_shares:
        less.append("成交量不足，為避免衝擊價格而少買")
    if "單檔上限" in p["cap_name"] and p["cap_val"] < raw_shares:
        less.append(f"已達{bcfg['name']}單檔上限，分散風險")
    a = c.get("alpha") or {}
    if basket == "offense":
        pred = c.get("prediction") or {}
        if pred and not pred.get("reliable"):
            less.append("此檔開盤預測模型回測命中率未達門檻，預測權重已降低")
        if a:
            (more if a["p_newhigh"] >= 0.6 else less).append(
                f"一個月內創新高機率 {a['p_newhigh'] * 100:.1f}%" + ("，勝率高可多配置" if a["p_newhigh"] >= 0.6 else "，勝率普通故保守配置"))
            rsi = (a.get("features") or {}).get("rsi14")
            if rsi and rsi > params.basket("offense")["anti_chase"]["pullback_rsi"]:
                less.append(f"RSI14 = {rsi:.1f} 偏熱，限價等回檔，可能買不到或買得少")
    elif a:
        (more if a["p_up"] >= 0.6 else less).append(
            f"一個月收正報酬機率 {a['p_up'] * 100:.1f}%" + ("，可提高「正報酬晉級」機率" if a["p_up"] >= 0.6 else "，勝率普通"))
        (more if a["exp_dd"] > -0.05 else less).append(f"預期最大回檔 {a['exp_dd'] * 100:.1f}%"
                                                       + ("，下檔有限" if a["exp_dd"] > -0.05 else "，下檔風險偏大"))
    rev = c.get("revenue") or {}
    if rev and rev.get("yoy", 0) > 0.2:
        more.append(f"月營收年增 {rev['yoy'] * 100:+.1f}%，基本面支撐")
    elif rev and rev.get("yoy", 0) < 0:
        less.append(f"月營收年減 {rev['yoy'] * 100:+.1f}%，基本面偏弱")
    if margin:
        more.append("使用融資（自備 40%）放大部位：進攻籃允許融資，且融資借款仍在總資產 30% 上限內")

    tp = bcfg["take_profit"]
    lots_tp1 = _round_sell(shares * tp[0][1], shares, st["allow_odd_lot"])
    exit_plan = [
        f"停損：收盤跌破 {stop:.2f} 元全數賣出 {fmt_shares(shares)}",
        f"第 1 段停利：漲至 {round_tick(entry * (1 + tp[0][0]), p['etf'])} 元（+{tp[0][0] * 100:.0f}%）賣出 {fmt_shares(lots_tp1)}，停損上移至成本",
        f"第 2 段停利：漲至 {round_tick(entry * (1 + tp[1][0]), p['etf'])} 元（+{tp[1][0] * 100:.0f}%）再賣剩餘的 {tp[1][1] * 100:.0f}%，其餘用移動停損（最高價 − 2×ATR）",
        f"訊號轉弱（{label} ≤ {st['sell_score_threshold']:+.2f}）先減碼一半",
        f"換股：同一籃出現扣除換股成本後預期報酬高出 {st['swap_margin'] * 100:.1f}% 以上的標的，依虧損補回計畫換股",
    ]
    if a and basket == "offense":
        exit_plan.insert(0, f"一個月目標：突破 52 週高點 {a['hi252']} 元（距現價 {-a['dist_hi'] * 100:.1f}%）即確認創新高，續抱讓獲利奔跑")
    rt = round_trip_cost_pct(code)
    why_stock = [c["pick_reason"]] + (c.get("def_reasons", []) if basket == "defense" else c["reasons"])
    risks = c.get("def_risks", []) if basket == "defense" else c["risks"]
    return {
        "action": "BUY", "code": code, "name": c["name"], "sector": c["sector"], "basket": basket,
        "basket_name": bcfg["name"], "shares": shares, "lots": lots, "odd": odd, "limit_price": entry, "stop": stop,
        "amount": round(amount), "est_cost": cost, "margin": margin, "score": p["score"],
        "components": c["def_components"] if basket == "defense" else c["components"],
        "weight_pct": round(amount / nav * 100, 2),
        "why_stock": why_stock, "risks": risks, "why_qty": why_qty,
        "why_more": more, "why_less": less, "exit_plan": exit_plan,
        "round_trip_cost_pct": round(rt, 3),
        "breakeven": round(entry * (1 + rt / 100), 2),
    }
