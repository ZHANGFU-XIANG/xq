"""所有輔助 Agent 的註冊（registry.run 會匯入這個模組）。每個函式只接受關鍵字參數，回傳 dict。

新增 Agent：寫一個 _xxx(**kw) 包裝，加上 @register(名稱, 中文說明, timeout=…, uses_llm=…, required=(必要欄位,))，
tests/test_agents_contract.py 會檢查每個 Agent 都有說明、逾時與 fallback，並用假資料跑過必要欄位。
"""
from __future__ import annotations

from src.agents.registry import register


@register("data_guard", "資料檢查：日 K 是否最新、和交易所是否一致", timeout=10, required=("level",))
def _data_guard(**kw) -> dict:
    from src.agents import data_guard

    return data_guard.STATE.get("last") or {"level": "unknown", "issues": []}


@register("risk", "風險實驗室：壓力測試、追繳點位、VaR、黑天鵝", timeout=120, required=("stress", "alerts"))
def _risk(**kw) -> dict:
    from src.agents import risk_lab

    return risk_lab.snapshot()


@register("calendar", "交易日曆：結算、除權息、處置股、競賽關鍵日", timeout=60, required=("events",))
def _calendar(**kw) -> dict:
    from src.agents import trade_calendar
    from src.state import portfolio as pfm

    pf = pfm.build_portfolio()
    return {"events": trade_calendar.events(list(pf.positions), list(pf.derivs))}


@register("overnight", "隔夜監控：夜盤、美股、持股開盤影響", timeout=120, required=("tx_night", "alerts"))
def _overnight(**kw) -> dict:
    from src.agents import overnight

    return overnight.snapshot()


@register("global", "國際市場：美日韓陸歐股、ADR、AI 半導體、商品匯率", timeout=120, required=("rows", "highlights"))
def _global(**kw) -> dict:
    from src.agents import global_markets

    return global_markets.run()


@register("industry", "產業新聞：持股與前 20 名所屬產業的利多利空", timeout=180, uses_llm=True, required=("sectors",))
def _industry(picks: list[str] | None = None, **kw) -> dict:
    from src.agents import industry_news

    return industry_news.run(picks or [])


@register("fact_check", "假消息過濾：來源分級、多來源證實、誇大與澄清", timeout=90, required=("items", "flagged"))
def _fact_check(**kw) -> dict:
    from src.agents import fact_check
    from src.api import data

    news = data.live_news().get("news", [])
    return fact_check.run([{"title": n["title"], "source": n["source"], "url": n["url"]} for n in news[:150]])


@register("forum", "論壇風向：PTT Stock 版提及與多空", timeout=120, required=("hot",))
def _forum(picks: list[str] | None = None, write_log: bool = False, **kw) -> dict:
    from src.agents import forum_sentiment

    return forum_sentiment.run(picks or [], write_log=write_log)


@register("debate", "多空辯論：只引用程式事實的多空論點與評審", timeout=300, uses_llm=True, required=("items",))
def _debate(**kw) -> dict:
    from src.agents import debate

    return debate.run()


ALL = ["data_guard", "risk", "calendar", "overnight", "global", "industry", "fact_check", "forum", "debate"]
FAST = ["data_guard", "risk", "calendar", "overnight", "global", "fact_check", "forum"]  # 不含 LLM 重的，網頁即時用
