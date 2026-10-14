"""PresentationAgent：決賽 6 分鐘簡報（佔總分 50%）的投影片、講稿與 Q&A，全部數字由程式從系統資料產生。

素材：淨值與加權比較、全歷史回測（含隔日開盤 vs 同日收盤的偏誤修正）、真實勝率表、風控（壓力測試、追繳點位、
黑天鵝研究）、多 Agent 架構、個股損益貢獻、影子組合差距拆解、重大問題的發現與修正。
講稿：每張投影片一段，模板組字（數字直接帶入，不經 LLM），總字數控制在 6 分鐘內（每分鐘約 260 字）。
輸出：reports/presentation/競賽簡報_日期.pptx（講稿放在備忘稿）與 講稿_日期.md；每週五週報後自動更新，11/27 後產生最終版。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from collections import defaultdict

from src.config import DATA_DIR, REPORT_DIR, now_tw, rules, stock_meta

log = logging.getLogger(__name__)
OUT = REPORT_DIR / "presentation"
CHARS_PER_MIN = 260
LIMIT_SEC = 360


def _contrib() -> list[dict]:
    """各標的損益貢獻（已實現＋未實現，含成本）。"""
    from src.agents import alerts, calc, on_demand
    from src.state import portfolio as pfm

    pf = pfm.build_portfolio()
    flow: dict[str, float] = defaultdict(float)
    for t in pfm.load_trades():
        if t.get("asset", "STOCK") not in ("STOCK", "ETF", ""):
            continue
        c = calc.cost(t["code"], t["side"], int(float(t["shares"])), float(t["price"]))
        flow[t["code"]] += c["net_cash"]
    rt = alerts.realtime(list(pf.positions))
    panel = on_demand.context()["panel"]
    out = []
    for code, cash in flow.items():
        p = pf.positions.get(code)
        px = ((rt.get(code) or {}).get("price") or (float(panel[code].Close.iloc[-1]) if code in panel else 0)) if p else 0
        out.append({"code": code, "name": stock_meta(code)["name"], "pnl": round(cash + (p.shares * px if p else 0)),
                    "held": bool(p)})
    return sorted(out, key=lambda x: -x["pnl"])


def facts() -> dict:
    from src.agents import full_backtest, registry, risk_lab, shadow, strategy_tuner
    from src.state import portfolio as pfm

    hist = pfm.load_history()
    initial = rules()["capital"]["initial"]
    nav = hist[-1]["nav"] if hist else initial
    f: dict = {"date": now_tw().date().isoformat(), "start": str(rules()["competition"]["start"]),
               "end": str(rules()["competition"]["end"]), "nav": nav, "ret": round((nav / initial - 1) * 100, 2),
               "nav_series": [{"date": h["date"], "ret": h["return_pct"]} for h in hist],
               "holding_ratio": hist[-1].get("holding_ratio") if hist else None,
               "min_ratio": min((h["holding_ratio"] for h in hist if h.get("holding_ratio") is not None), default=None)}
    try:
        from src.agents import on_demand

        tx = on_demand.context()["taiex"].Close
        s = tx[tx.index >= str(f["nav_series"][0]["date"])] if f["nav_series"] else tx
        base = float(tx[tx.index < str(f["start"])].iloc[-1])
        f["taiex_series"] = [{"date": str(d.date()), "ret": round((float(v) / base - 1) * 100, 2)} for d, v in s.items()]
        f["taiex_ret"] = f["taiex_series"][-1]["ret"] if f["taiex_series"] else None
    except Exception as e:  # noqa: BLE001
        log.info("簡報：加權比較失敗 %s", e)
        f["taiex_series"], f["taiex_ret"] = [], None
    pf = pfm.build_portfolio()
    f["trades"], f["symbols"] = pf.trade_count, len(pf.traded_symbols)
    fh = full_backtest.result()
    f["backtest"] = {"from": (fh.get("next_open") or {}).get("from"), "to": (fh.get("next_open") or {}).get("to"),
                     "open_cagr": (fh.get("next_open") or {}).get("cagr"), "close_cagr": (fh.get("close_same_day") or {}).get("cagr"),
                     "taiex_cagr": ((fh.get("next_open") or {}).get("taiex") or {}).get("cagr"),
                     "win38": ((fh.get("next_open") or {}).get("window") or {}).get("p_positive"),
                     "max_dd": (fh.get("next_open") or {}).get("max_dd"), "calibration": fh.get("calibration")}
    sc = strategy_tuner.scorecard().get("algorithms", [])
    f["scorecard_5y"] = next((a["value"] for a in sc if a.get("name", "").startswith("投資組合")), None)
    f["params"] = strategy_tuner.current_pf()
    try:
        r = risk_lab.snapshot()
        f["stress"] = r["stress"]
        f["margin"] = r.get("margin")
        f["swan"] = [{"name": b["name"], "label": b["label"]} for b in r["black_swan"]["items"]]
    except Exception as e:  # noqa: BLE001
        log.info("簡報：風險失敗 %s", e)
    try:
        sh = shadow.run()
        f["shadow"] = {"last": sh["series"][-1], "diff": sh["diff"], "breakdown": sh["breakdown"]}
    except Exception as e:  # noqa: BLE001
        log.info("簡報：影子組合失敗 %s", e)
    try:
        f["contrib"] = _contrib()
    except Exception as e:  # noqa: BLE001
        log.info("簡報：損益貢獻失敗 %s", e)
        f["contrib"] = []
    registry._load_all()
    f["agents"] = [{"name": n, "title": s.title, "llm": s.uses_llm} for n, s in registry.REGISTRY.items()]
    return f


def _pct(v, d=1):
    return "—" if v is None else f"{v * 100:+.{d}f}%"


def script(f: dict) -> list[dict]:
    """[{title, seconds, text}]：每段由事實組字；總長超過 6 分鐘時刪減選讀段落。"""
    bt, sh = f.get("backtest") or {}, f.get("shadow") or {}
    cal = (bt.get("calibration") or {}).get("buckets") or []
    top1 = next((b for b in cal if b["rank"] == "1"), None)
    best = (f.get("contrib") or [None])[0]
    worst = (f.get("contrib") or [None])[-1]
    s5 = next((s for s in f.get("stress") or [] if s["scenario"] == "加權指數 −5%"), None)
    br = {b["name"][:4]: b["amount"] for b in sh.get("breakdown", [])}
    secs = [
        ("封面", f"各位評審好，我們是用多 Agent 資料驅動系統參賽的隊伍。競賽期間總資產 {f['nav']:,} 元，累積報酬 {f['ret']:+.2f}%。"),
        ("成果總覽", (f"同期加權指數 {f['taiex_ret']:+.2f}%。" if f.get("taiex_ret") is not None else "")
         + f"我們共成交 {f['trades']} 筆、{f['symbols']} 個標的，"
         + (f"每天收盤持股率都在規定的 70% 以上（最低 {f['min_ratio'] * 100:.1f}%）。" if f.get("min_ratio", 0) >= 0.7
            else f"持股率最低曾到 {f['min_ratio'] * 100:.1f}%。" if f.get("min_ratio") is not None else "")),
        ("策略", f"核心是全市場一個月選股模型，每天排名，集中持有前 {f['params']['max_positions']} 檔；持有滿 {f['params']['min_hold_days']} 個交易日、"
                 f"出現扣成本後更好的股票就全部換股，最長抱 {f['params']['expiry_days']} 個交易日。"),
        ("驗證", f"我們用 {bt.get('from', '')[:4]} 到 {bt.get('to', '')[:4]} 年所有查得到的資料做樣本外回測，年化 {_pct(bt.get('open_cagr'))}、"
                 f"同期大盤 {_pct(bt.get('taiex_cagr'))}。過程中我們發現原本假設收盤價成交，年化 {_pct(bt.get('close_cagr'))} 是高估的，"
                 "改成實際做得到的隔日開盤成交才是真實數字。"),
        ("誠實的勝率", f"單一推薦 20 天後賺錢的機率約 {top1['win'] * 100:.0f}%，優勢來自賺多賠少，所以我們分散持有、嚴守停損，"
                     "不宣稱任何 90% 準確率。" if top1 else "我們公開模型的真實勝率，不宣稱高準確率。"),
        ("系統架構", f"系統由 {len(f.get('agents') or [])} 個 Agent 分工：資料檢查、風險、國際市場、產業新聞、假消息過濾、論壇風向、財報、"
                     "市場異常、多空辯論、影子組合與工程師監控；所有數字由程式計算，語言模型只負責分類與文字，並經過數字核對。"),
        ("風控", (f"每天做壓力測試：大盤跌 5% 估計影響 {s5['total']:,} 元，占總資產 {s5['pct_nav']}%；" if s5 else "")
         + (f"期貨距追繳點位 {f['margin']['pct_to_call']}%，系統即時推播。" if f.get("margin") else "")
         + "黑天鵝指標經 26 年回測沒有預測力，我們只當提醒、不用來恐慌賣出。"),
        ("交易檢討", (f"貢獻最大的是 {best['name']}，{best['pnl']:+,} 元；" if best else "")
         + (f"拖累最大的是 {worst['name']}，{worst['pnl']:+,} 元。" if worst and worst is not best else "")),
        ("影子組合", f"我們讓系統另開一個完全照策略、開盤成交的影子帳戶。實際比影子差 {sh['diff']:+,} 元，"
                     f"主要來自期貨 {br.get('期貨部位', 0):+,} 元與成交時機 {br.get('成交時機', 0):+,} 元，這是我們的改進方向。"
         if sh.get("breakdown") else ""),
        ("發現與修正", "我們建立自動檢查：發現盤後報告沿用盤中舊資料、回測假設不切實際、現金與官方差 1 元，"
                      "每一個都找到原因、修正並加上測試，避免再犯。"),
        ("結論", "我們的重點不是預測多準，而是用可驗證的流程做決策：數字可追溯、風險可量化、錯誤能被系統發現。謝謝各位。"),
    ]
    out = [{"title": t, "text": x, "seconds": round(len(x) / CHARS_PER_MIN * 60)} for t, x in secs if x]
    optional = ["系統架構", "交易檢討", "發現與修正"]
    while sum(o["seconds"] for o in out) > LIMIT_SEC and optional:
        drop = optional.pop()
        out = [o for o in out if o["title"] != drop]
    return out


def qa(f: dict) -> list[dict]:
    bt = f.get("backtest") or {}
    cal = (bt.get("calibration") or {})
    return [
        {"q": "模型準確率多少？", "a": f"樣本外排名第 1 名 20 天後賺錢約 {((cal.get('buckets') or [{}])[0].get('win') or 0) * 100:.0f}%，"
                                   f"前 5 名分散約 {(cal.get('top5_portfolio_win') or 0) * 100:.0f}%；優勢在賺多賠少。"},
        {"q": "怎麼避免過度擬合？", "a": "參數只在前半段挑、後半段不能變差才採用；全歷史回測、隔日開盤成交、競賽成本；近 5 年模擬太敏感的參數不自動採用。"},
        {"q": "期貨槓桿怎麼控管？", "a": (f"每天計算追繳點位（目前距 {f['margin']['pct_to_call']}%）與大盤 −3／−5／−10% 壓力測試，即時推播。"
                                     if f.get("margin") else "每天計算追繳點位與壓力測試並推播。")},
        {"q": "LLM 會不會亂講數字？", "a": "數字全部由程式計算，LLM 只做新聞分類與措辭，輸出經 Verifier 比對事實中的數字，對不上就刪除或改用模板。"},
        {"q": "回測和實際差多少？", "a": f"回測年化 {_pct(bt.get('open_cagr'))}；實際報酬 {f['ret']:+.2f}%，影子組合拆解出期貨與成交時機是主要差距。"},
        {"q": "遇到資料錯誤怎麼辦？", "a": "資料檢查 Agent 每次比對交易所即時資料，自動補正；持股缺價格時報告標示勿依此下單、不寫淨值。"},
        {"q": "為什麼持有這麼多檔？", "a": "早期建倉分散在多個穩健標的；系統已標示零碎部位，換股時一併整併到前 5 名。"},
        {"q": "黑天鵝怎麼處理？", "a": "VIX、高收益債、日圓、台幣等指標 26 年回測沒有預測力，只做提醒；真正的保護是停損與追繳預警。"},
        {"q": "論壇與新聞有用嗎？", "a": "論壇累積 60 個交易日前只當參考；新聞經假消息過濾（來源分級、多來源證實）後才進入判斷。"},
        {"q": "如果重來會改什麼？", "a": "降低期貨槓桿、開盤前預掛單減少滑價、從一開始就集中 5 檔，影子組合已量化這些改進的金額。"},
    ]


def build(final: bool = False) -> dict:
    from src.reports import presentation_pptx

    f = facts()
    sc = script(f)
    q = qa(f)
    OUT.mkdir(parents=True, exist_ok=True)
    tag = "最終版" if final else f["date"]
    pptx = presentation_pptx.build(f, sc, q, OUT / f"競賽簡報_{tag}.pptx")
    md = OUT / f"講稿_{tag}.md"
    total = sum(s["seconds"] for s in sc)
    md.write_text(f"# 決賽簡報講稿（約 {total // 60} 分 {total % 60} 秒）\n\n"
                  + "\n\n".join(f"## {i + 1}. {s['title']}（約 {s['seconds']} 秒）\n{s['text']}" for i, s in enumerate(sc))
                  + "\n\n# Q&A 預備\n\n" + "\n\n".join(f"**問：{x['q']}**\n答：{x['a']}" for x in q), encoding="utf-8")
    (DATA_DIR / "presentation.json").write_text(json.dumps({"date": f["date"], "pptx": pptx.relative_to(REPORT_DIR).as_posix(),
                                                             "script": md.relative_to(REPORT_DIR).as_posix(), "seconds": total,
                                                             "slides": len(sc), "sections": sc, "qa": q},
                                                            ensure_ascii=False, indent=1), encoding="utf-8")
    return {"pptx": pptx, "script": md, "seconds": total}


def latest() -> dict:
    p = DATA_DIR / "presentation.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
