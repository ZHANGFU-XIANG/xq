"""Orchestrator：依時段串接各 Agent。

用法：
  python -m src.main premarket [--date YYYY-MM-DD] [--force]   盤前報告（07:00）
  python -m src.main preopen                                    開盤前更新（08:20，加入那指期即時報價）
  python -m src.main intraday                                   盤中每小時建議（09:05～13:05，即時報價）
  python -m src.main postclose                                  盤後檢討＋調整 Agent 健康檢查＋隔日預掛單（15:45）
  python -m src.main weekly                                     週報＋完整模型調校
  python -m src.main tune [--rollback]                          手動執行調整 Agent／回復上一版參數
  python -m src.main record --code 2330 --side BUY --shares 1000 --price 1000 [--margin] [--date ...]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import sys

import pandas as pd

from src import pipeline
from src.agents import (alerts, alpha_model, holding_advice, attribution, compliance, filings, macro, market_data, news, recovery, sizer, tuner,
                        writer)
from src.config import DATA_DIR, FACTS_DIR, REPORT_DIR, is_trading_day, next_trading_day, now_tw, rules, stock_meta
from src.llm.client import get_client
from src.reports import charts, docx_builder, emailer, pptx_builder
from src.state import params
from src.state import portfolio as pfm

log = logging.getLogger("orchestrator")
PENDING_JSON = DATA_DIR / "pending_orders.json"
TITLES = {"premarket": "盤前報告", "preopen": "開盤前更新", "postclose": "盤後檢討與隔日預掛單"}


def _update_meta(pf: pfm.Portfolio, feats: dict) -> dict:
    """更新停損、停利階段、進場後最高價（移動停損）。"""
    st = params.strategy()
    meta = pfm.load_meta()
    pending = json.loads(PENDING_JSON.read_text(encoding="utf-8")) if PENDING_JSON.exists() else {}
    for code in list(meta):
        if code not in pf.positions:
            del meta[code]
    for code, pos in pf.positions.items():
        f = feats.get(code)
        if not f:
            continue
        m = meta.get(code)
        if m is None:
            stop = pending.get(code, {}).get("stop") or round(pos.avg_cost - st["atr_mult_stop"] * f["atr14"], 2)
            m = {"stop": stop, "tp_stage": 0, "high": f["close"], "shares": pos.shares,
                 "basket": pending.get(code, {}).get("basket")}
        if pos.shares < m.get("shares", pos.shares):  # 有部分賣出 → 視為完成一段停利
            m["tp_stage"] = min(m.get("tp_stage", 0) + 1, len(params.basket(m.get("basket") or "offense")["take_profit"]))
        m["shares"] = pos.shares
        m["high"] = max(m.get("high", f["close"]), f["close"])
        if m["tp_stage"] >= 1:  # 停利後：停損至少保本，並以最高價 − 2ATR 追蹤
            m["stop"] = round(max(m["stop"], pos.avg_cost, m["high"] - st["atr_mult_stop"] * f["atr14"]), 2)
        meta[code] = m
    pfm.save_meta(meta)
    return meta


def _market_context(target: dt.date) -> dict:
    return pipeline.market_context(target)


def run_tuner(ctx: dict, today: dt.date, target: dt.date, nav: float | None, today_pnls: dict, full: bool) -> dict:
    """調整 Agent：對答案 → 健康檢查 → （必要時）修正參數。"""
    ev = tuner.evaluate(ctx["panel"], ctx["taiex"])
    issues = tuner.health(ev, nav, today_pnls)
    types = {i["type"] for i in issues}
    changes = []
    if full or "overnight" in types:
        ch = tuner.tune_overnight(ctx["taiex"], ctx["us_hist"], target)
        if ch:
            ch["reason"] = "隔夜模型失準或例行調校：近 60 日命中率較佳的參數組合"
            changes.append(ch)
    if full or "alpha" in types:
        ch = tuner.tune_alpha(lambda: alpha_model.build_panel(ctx["panel"], ctx["codes"], ctx["taiex"].Close, target,
                                                              ctx["etf"]))
        if ch:
            ch["reason"] = "一個月模型失準或例行調校：滾動回測分數較佳的超參數"
            changes.append(ch)
        w = tuner.tune_weights(ev)
        if w:
            changes.append(w)
    if full:
        a = tuner.tune_allocation(ev)
        if a:
            changes.append(a)
    changes += tuner.tune_risk(issues)
    note = writer.postmortem({"issues": issues, "changes": [{k: v for k, v in c.items() if k != "trials"} for c in changes],
                              "stats": tuner.summary_stats(ev)}) if (issues or changes) else ""
    tuner.apply(changes, issues, today, note)
    return {"stats": tuner.summary_stats(ev), "issues": issues, "changes": changes, "note": note,
            "recent_log": [e for e in tuner.history() if e.get("changes")][-5:]}


def run_daily(mode: str, today: dt.date) -> dict:
    gen = now_tw()
    target = today if (mode != "postclose" and is_trading_day(today)) else next_trading_day(today)
    log.info("模式 %s，報告日 %s，預測目標交易日 %s", mode, today, target)
    st = params.strategy()

    # ---- 資料層：全市場 + 一個月模型
    ctx = _market_context(target)
    us_hist, panel, alpha_out = ctx["us_hist"], ctx["panel"], ctx["alpha"]
    a_preds = alpha_out["predictions"]
    pf = pfm.build_portfolio()
    cand_codes = pipeline.candidate_codes(ctx, pf)

    news_items = news.fetch_news(gen)
    senti = news.analyze_sentiment(news_items, {c: stock_meta(c)["name"] for c in cand_codes})
    macro_events = macro.extract_events(news_items)
    filing_sum = filings.summarize_filings()

    # ---- 分析層
    sc = pipeline.score_codes(cand_codes, ctx, target, senti["by_code"])
    feats, prices, preds, ranked, scores = sc["feats"], sc["prices"], sc["preds"], sc["ranked"], sc["scores"]
    us_snap = sc["us_snap"]

    # 今日漲跌歸因（持股；尚無持股時改為前 5 名候選）
    att_targets = {c: p.shares for c, p in pf.positions.items()} or {s["code"]: 0 for s in ranked[:5]}
    us_prev = {r["ticker"]: r for r in market_data.us_snapshot(us_hist, (ctx["taiex"][ctx["taiex"].index < pd.Timestamp(target)].index[-1]).date())}
    att = attribution.attribute(att_targets, panel, ctx["taiex"], ctx["codes"], pd.Timestamp(target), us_prev, senti["by_code"])
    today_pnls = {r["code"]: r["pnl"] for r in att["rows"] if r["shares"]}

    # ---- 決策層
    meta = _update_meta(pf, feats)
    nav = pf.nav(prices)
    basket_of = pipeline.basket_of(pf, meta, scores)
    pfm.save_meta(meta)
    alloc = pipeline.current_allocation(ctx, target, today, nav)
    rec = recovery.plan(pf, prices, scores, a_preds, meta, ranked, nav, rules()["capital"]["initial"], today_pnls,
                        basket_of)
    sens_codes = list(pf.positions) or [s["code"] for s in ranked[:5]]
    sens = macro.sensitivities(sens_codes, panel, us_hist, pd.Timestamp(target))
    hold_mv = {c: p.shares * prices.get(c, 0) for c, p in pf.positions.items()} or \
              {s["code"]: nav * 0.1 for s in ranked[:5]}  # 無持股時以每檔 10% 假設部位試算
    macro_imp = macro.impacts(macro_events, hold_mv, sens, nav, {c: m.get("stop") for c, m in meta.items()}, prices)

    sells = sizer.plan_sells(pf, prices, feats, scores, meta, nav, rec["actions"])
    buys = sizer.plan_buys(ranked, pf, prices, nav, sells, feats, alloc, basket_of)
    orders = [o for o in sells + buys["orders"] if not compliance.is_warrant(o["code"])]
    checks = compliance.check(pf, prices, orders, today, nav)
    PENDING_JSON.write_text(json.dumps({o["code"]: {"stop": o.get("stop"), "date": target.isoformat(), "basket": o["basket"]}
                                        for o in orders if o["action"] == "BUY"}, ensure_ascii=False), encoding="utf-8")

    # ---- 調整 Agent
    if mode == "premarket":
        tuner.log_predictions(today, target, preds, alpha_out, ranked, list(pf.positions))
    tune = run_tuner(ctx, today, target, nav, today_pnls, full=False) if mode == "postclose" else {
        "stats": tuner.summary_stats(tuner.evaluate(panel, ctx["taiex"])), "issues": [], "changes": [], "note": "",
        "recent_log": [e for e in tuner.history() if e.get("changes")][-5:]}

    mv_after = pf.market_value(prices) + sum(
        o["shares"] * o["limit_price"] * (1 if o["action"] == "BUY" else -1) for o in orders)
    positions = [{"code": c, "name": stock_meta(c)["name"], "basket": params.basket(basket_of.get(c, "offense"))["name"],
                  "shares": p.shares, "avg_cost": round(p.avg_cost, 2),
                  "price": prices.get(c, 0), "pnl_pct": round((prices.get(c, p.avg_cost) / p.avg_cost - 1) * 100, 2),
                  "weight_pct": round(p.shares * prices.get(c, 0) / nav * 100, 2)} for c, p in pf.positions.items()]
    if mode == "postclose":
        pfm.record_history(today.isoformat(), nav, pf.holding_ratio(prices))
    history = pfm.load_history()
    nq = market_data.nq_live() if mode == "preopen" else None
    top_picks = [{"code": s["code"], "name": s["name"], "sector": s["sector"], "score": s["score"],
                  "p_newhigh": s["alpha"]["p_newhigh"], "exp_excess": s["alpha"]["exp_excess"],
                  "dist_hi": s["alpha"]["dist_hi"], "signal": s["signal"],
                  "rev_yoy": (s.get("revenue") or {}).get("yoy"), "overheat": s.get("overheat")}
                 for s in ranked if s.get("alpha")][:20]
    def_picks = [{"code": s["code"], "name": s["name"], "sector": s["sector"], "def_score": s["def_score"],
                  "p_up": s["alpha"]["p_up"], "exp_dd": s["alpha"]["exp_dd"], "beta": (s["alpha"]["features"] or {}).get("beta60"),
                  "vol20": (s["alpha"]["features"] or {}).get("vol20"), "signal": s["def_signal"]}
                 for s in sorted(ranked, key=lambda x: -x.get("def_score", -9)) if s.get("alpha") and s.get("eligible_def")][:15]

    try:
        alert_out = {k: v for k, v in alerts.collect(pf, meta, orders, us_snap, target).items() if k != "realtime"}
    except Exception as e:  # noqa: BLE001  警示失敗不影響報告
        log.warning("警示產生失敗：%s", e)
        alert_out = {"alerts": [], "counts": {}, "limit_up": [], "limit_down": [], "up_industries": [],
                     "down_industries": [], "limit_up_count": 0, "limit_down_count": 0, "attention_watch": []}

    # 持股操作建議：風險警示的賣出併入訂單後，每檔給出 加碼／續抱／減碼／全部出清
    orders = holding_advice.merge_alert_sells(orders, alert_out.get("alerts", []), pf, prices, basket_of)
    checks = compliance.check(pf, prices, orders, today, nav)
    mv_after = pf.market_value(prices) + sum(
        o["shares"] * o["limit_price"] * (1 if o["action"] == "BUY" else -1) for o in orders)
    advice = holding_advice.build(pf, prices, scores, a_preds, meta, orders, alert_out.get("alerts", []), basket_of, nav)

    facts = {
        "title": TITLES[mode], "mode": mode, "date": today.isoformat(), "target_date": target.isoformat(),
        "generated_at": gen.strftime("%Y-%m-%d %H:%M"),
        "names": {c: stock_meta(c)["name"] for c in feats},
        "us_snapshot": us_snap, "nq_live": nq, "predictions": preds,
        "market_view": senti["market_view"], "events": senti["events"],
        "news": [{k: n[k] for k in ("id", "source", "title", "url", "time")} for n in news_items],
        "filings": filing_sum,
        "alpha_model": {k: alpha_out[k] for k in ("asof", "n_stocks", "n_train", "base_rate", "mkt_mean20", "metrics",
                                                  "feature_ic", "hyperparams")},
        "top_picks": top_picks, "def_picks": def_picks,
        "allocation": {**alloc, "baskets": buys["baskets"]},
        "ranking": [{k: s[k] for k in ("code", "name", "score", "signal", "components")} for s in ranked],
        "attribution": att, "recovery": {k: rec[k] for k in ("items", "summary", "portfolio_exp_ret")},
        "macro": macro_imp, "macro_sens": sens,
        "orders": orders, "compliance": checks, "tuner": tune, "alerts": alert_out, "holding_advice": advice,
        "funding": {"transfer_from_futures": buys["transfer_from_futures"], "futures_reserve": st["futures_reserve"],
                    "cash_after": buys["cash_after"], "loan_capacity_left": buys["loan_capacity_left"]},
        "portfolio": {"nav": round(nav), "return_pct": round((nav / rules()["capital"]["initial"] - 1) * 100, 3),
                      "holding_ratio": round(pf.holding_ratio(prices), 4), "trade_count": pf.trade_count,
                      "cash": round(pf.cash), "positions": positions},
        "projected_ratio": round(mv_after / nav, 4) if nav else 0,
        "hypothetical": not pf.positions,
    }
    text = writer.write(facts)
    if nq:
        text["outlook"] += f"（08:20 更新：那斯達克期貨 {nq['price']}，{nq['change_pct']:+.2f}%）"
    if mode == "postclose":
        text["outlook"] += "（注意：盤後版使用的美股資料為前一晚收盤，明早 07:00 盤前報告會以最新美股重新計算）"

    # ---- 產出層
    stem = f"{today.isoformat()}_{TITLES[mode]}"
    out_dir = REPORT_DIR / today.strftime("%Y-%m")
    img_dir = out_dir / "img"
    img_dir.mkdir(parents=True, exist_ok=True)
    weights = [(f"{p['code']} {p['name']}（{p['basket'][:2]}）", p["weight_pct"]) for p in positions]
    weights += [(f"{o['code']} {o['name']}（新・{o['basket_name'][:2]}）", o["weight_pct"]) for o in orders if o["action"] == "BUY"]
    ch = {
        "us": charts.us_moves_chart(us_snap, img_dir / f"{stem}_us.png") if us_snap else None,
        "pred": charts.prediction_chart(preds, facts["names"], img_dir / f"{stem}_pred.png") if preds else None,
        "allocation": charts.allocation_chart(weights, img_dir / f"{stem}_alloc.png") if weights else None,
        "nav": charts.nav_chart(history, img_dir / f"{stem}_nav.png"),
        "attribution": charts.attribution_chart(att, img_dir / f"{stem}_att.png") if att["rows"] else None,
        "top_picks": charts.top_picks_chart(top_picks, alpha_out["base_rate"], img_dir / f"{stem}_top.png") if top_picks else None,
        "sectors": charts.sector_chart(att, img_dir / f"{stem}_sector.png") if att.get("sector_median_pct") else None,
    }
    docx_path = docx_builder.build_daily(facts, text, ch, out_dir / f"{stem}.docx")
    pptx_path = pptx_builder.build_daily(facts, text, ch, out_dir / f"{stem}.pptx")
    FACTS_DIR.mkdir(parents=True, exist_ok=True)
    (FACTS_DIR / f"{stem}.json").write_text(json.dumps({**facts, "text": text}, ensure_ascii=False, indent=1, default=str),
                                             encoding="utf-8")
    orders_csv = out_dir / f"{stem}_下單清單.csv"
    with orders_csv.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["動作", "代號", "名稱", "整股張數", "零股股數", "總股數", "限價", "停損", "融資", "金額"])
        for o in orders:
            w.writerow(["買進" if o["action"] == "BUY" else "賣出", o["code"], o["name"], o["lots"], o["odd"],
                        o["shares"], o["limit_price"], o.get("stop", ""), "是" if o.get("margin") else "否", o["amount"]])

    advice_lines = [f"- {h['code']} {h['name']}：{h['verdict']}，{h['qty_text']}"
                    + (f"（限價 {h['limit_price']}）" if h["limit_price"] else "") for h in advice]
    alert_lines = [f"- [{a['level']}] {a['title']}：{a['reasons'][0]} → {a['action']}"
                   for a in alert_out["alerts"] if a["level"] in ("嚴重", "警告")]
    body = "\n".join([f"{TITLES[mode]} {today}", ""] + (["【庫存怎麼處理】"] + advice_lines + [""] if advice_lines else []) +
                     (["【警示】"] + alert_lines + [""] if alert_lines else []) +
                     [text["market_summary"], text["outlook"], "",
                      f"【攻守配置】進攻籃 {alloc['offense'] * 100:.0f}%／穩健籃 {alloc['defense'] * 100:.0f}%",
                      text.get("allocation_analysis", ""), "",
                      "【今日漲跌分析】", text["daily_move_analysis"], "", "【虧損補回計畫】", text["recovery_plan"], "",
                      "【國際大事影響】", text["macro_analysis"], "", "交易建議："] +
                     [f"- {'買進' if o['action'] == 'BUY' else '賣出'}【{params.basket(o['basket'])['name']}】"
                      f"{o['code']} {o['name']} {o['lots']} 張"
                      + (f"+{o['odd']} 股" if o["odd"] else "") + f" 限價 {o['limit_price']}" for o in orders] +
                     ["", "規則檢查："] + [f"- [{c['status']}] {c['item']}：{c['detail']}" for c in checks] +
                     (["", "調整 Agent：" + tune["note"]] if tune.get("note") else []))
    emailer.send(f"[投資競賽] {TITLES[mode]} {today}", body, [docx_path, pptx_path, orders_csv])
    log.info("完成：%s / %s（LLM 呼叫 %d 次，文字來源 %s）", docx_path.name, pptx_path.name, get_client().calls, text["source"])
    print(body)
    return facts


def run_weekly(today: dt.date) -> None:
    target = next_trading_day(today)
    ctx = _market_context(target)
    tune = run_tuner(ctx, today, target, None, {}, full=True)
    history = pfm.load_history()
    pf = pfm.build_portfolio()
    week_start = today - dt.timedelta(days=today.weekday())
    trades = [t for t in pf.trades if t["date"] >= week_start.isoformat()]
    out_dir = REPORT_DIR / "weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    nav_png = charts.nav_chart(history, out_dir / f"{today}_nav.png")
    d = docx_builder.Doc()
    d.title("台股投資競賽　週報", f"{week_start} ~ {today}")
    last = history[-1] if history else {"nav": rules()["capital"]["initial"], "return_pct": 0, "holding_ratio": 0}
    d.table(["總資產", "累積報酬", "持股率", "累計交易筆數", "累計交易標的"],
            [[f"{last['nav']:,}", f"{last['return_pct']:+.2f}%", f"{last['holding_ratio'] * 100:.1f}%",
              pf.trade_count, len(pf.traded_symbols)]], color_col=1)
    d.image(nav_png)
    d.h1("本週交易紀錄")
    if trades:
        d.table(["日期", "代號", "方向", "股數", "價格", "融資", "備註"],
                [[t["date"], t["code"], t["side"], t["shares"], t["price"], t.get("margin", ""), t.get("note", "")]
                 for t in trades])
    else:
        d.para("本週無交易。")
    docx_builder.tuner_section(d, tune, ctx["alpha"])
    d.h1("本週各日報告決策摘要")
    for fpath in sorted(FACTS_DIR.glob("*.json")):
        if fpath.name[:10] >= week_start.isoformat() and "盤前報告" in fpath.name:
            fx = json.loads(fpath.read_text(encoding="utf-8"))
            d.h2(fx["date"])
            d.para(fx["text"]["outlook"])
            d.bullets([f"{'買進' if o['action'] == 'BUY' else '賣出'} {o['code']} {o['name']} {o['shares']:,} 股"
                       for o in fx["orders"]] or ["無交易建議"])
    path = out_dir / f"{today}_週報.docx"
    d.save(path)
    deck = pptx_builder.Deck(f"週報 {today}")
    deck.title_slide("台股投資競賽 週報", f"{week_start} ~ {today}")
    deck.kpis("本週績效", [("總資產", f"{last['nav'] / 1e4:,.0f} 萬", None), ("累積報酬", f"{last['return_pct']:+.2f}%", None),
                       ("交易筆數", str(pf.trade_count), None), ("交易標的", str(len(pf.traded_symbols)), None)],
              [f"本週交易 {len(trades)} 筆"] + ([tune["note"]] if tune["note"] else []))
    deck.image("累積報酬走勢", nav_png)
    pptx_builder.tuner_slide(deck, tune)
    ppt = out_dir / f"{today}_週報.pptx"
    deck.save(ppt)
    emailer.send(f"[投資競賽] 週報 {today}", f"週報 {week_start} ~ {today}\n\n調整 Agent：{tune['note'] or '無需調整'}", [path, ppt])
    print(f"週報完成：{path}")


def main(argv=None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["premarket", "preopen", "intraday", "postclose", "weekly", "tune", "record"])
    ap.add_argument("--date")
    ap.add_argument("--force", action="store_true", help="非交易日也執行")
    ap.add_argument("--rollback", action="store_true", help="tune：回復上一次調整前的參數")
    ap.add_argument("--code")
    ap.add_argument("--side", choices=["BUY", "SELL"])
    ap.add_argument("--shares", type=int)
    ap.add_argument("--price", type=float)
    ap.add_argument("--margin", action="store_true")
    ap.add_argument("--note", default="")
    a = ap.parse_args(argv)
    today = dt.date.fromisoformat(a.date) if a.date else now_tw().date()

    if a.mode == "record":
        if compliance.is_warrant(a.code):
            sys.exit("權證不可交易（競賽規則）")
        pfm.append_trades([{"date": today.isoformat(), "code": a.code, "side": a.side, "shares": a.shares,
                            "price": a.price, "margin": int(a.margin), "note": a.note}])
        pf = pfm.build_portfolio()
        print(f"已記錄。現金 {pf.cash:,.0f}，交易筆數 {pf.trade_count}，持有 {len(pf.positions)} 檔")
        return
    if a.mode == "tune":
        if a.rollback:
            d = tuner.rollback()
            print(f"已回復到 {d} 調整前的參數" if d else "沒有可回復的調整紀錄")
            return
        target = next_trading_day(today)
        res = run_tuner(_market_context(target), today, target, None, {}, full=True)
        print(json.dumps({k: res[k] for k in ("stats", "issues", "note")}, ensure_ascii=False, indent=1))
        for c in res["changes"]:
            print("修改", c["param"], c.get("before"), "→", c.get("after"))
        return
    if a.mode == "weekly":
        run_weekly(today)
        return
    if a.mode == "intraday":
        if not is_trading_day(today) and not a.force:
            print(f"{today} 非交易日，略過")
            return
        from src.agents import intraday
        from src.api import data as apidata

        out = intraday.run(apidata.load_report())
        intraday.save(out)
        body = intraday.email_body(out)
        emailer.send(f"[投資競賽] 盤中建議 {out['time'][11:]}｜{out['summary']}", body, [])
        print(body)
        return
    if not is_trading_day(today) and not a.force:
        print(f"{today} 非交易日，略過（加 --force 可強制執行）")
        return
    run_daily(a.mode, today)


if __name__ == "__main__":
    main()
