"""用假的 LLM 驗證防幻覺流程：捏造數字會被退回，最後改用模板；正確內容則採用。"""
from src.agents import news, writer


class FakeClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def available(self):
        return True

    def structured(self, prompt, schema, system="", pdf=None):
        self.calls += 1
        return schema.model_validate(self.outputs.pop(0))


FACTS = {
    "date": "2026-10-05",
    "us_snapshot": [{"ticker": "^SOX", "name": "費城半導體", "change_pct": 2.4, "close": 13136.67, "chg_5d_pct": 3.1,
                     "date": "2026-10-02"}],
    "market_view": "", "events": [], "compliance": [],
    "predictions": {"TAIEX": {"gap_pct": 0.69, "direction": "開高", "intraday_pct": 0.43, "after_open": "續強",
                              "gap_hit_rate": 0.802, "gap_band_pct": 0.52}},
    "orders": [{"action": "BUY", "code": "3037", "name": "欣興", "shares": 1463, "limit_price": 1345.0, "stop": 1210.0}],
}


def test_writer_rejects_hallucination_then_falls_back(monkeypatch):
    bad = {"market_summary": "費半大漲 5.8%", "outlook": "目標指數 30000", "allocation_analysis": "x", "daily_move_analysis": "x",
           "recovery_plan": "x", "macro_analysis": "x", "order_notes": [{"code": "3037", "note": "x"}]}
    fake = FakeClient([bad, bad, bad])
    monkeypatch.setattr(writer, "get_client", lambda: fake)
    out = writer.write(FACTS)
    assert out["source"] == "template" and fake.calls == 3


def test_writer_accepts_grounded_text(monkeypatch):
    good = {"market_summary": "費城半導體上漲 2.4%。", "outlook": "預測開盤 +0.69%，命中率 80.2%。",
            "allocation_analysis": "進攻與穩健兩籃配置。", "daily_move_analysis": "尚無持股。", "recovery_plan": "無虧損。", "macro_analysis": "無重大事件。",
            "order_notes": [{"code": "3037", "note": "買進 1,463 股，限價 1345，停損 1210。"}]}
    monkeypatch.setattr(writer, "get_client", lambda: FakeClient([good]))
    assert writer.write(FACTS)["source"] == "llm-verified"


def test_sentiment_drops_unsourced_and_invented(monkeypatch):
    items = [{"id": 1, "source": "鉅亨網", "title": "欣興 ABF 載板接單滿", "summary": "產能利用率達 90%", "url": "u1", "time": ""},
             {"id": 2, "source": "中央社", "title": "欣興擴產", "summary": "", "url": "u2", "time": ""}]
    res = {"market_view": "市場偏多", "events": [
        {"stock_codes": ["3037"], "sentiment": 2, "event": "欣興產能利用率 90%", "news_ids": [1, 2]},
        {"stock_codes": ["3037"], "sentiment": 2, "event": "欣興營收將成長 45%", "news_ids": [1]},   # 捏造數字
        {"stock_codes": ["9999"], "sentiment": 1, "event": "不在清單", "news_ids": [1]},             # 代號不在清單
        {"stock_codes": ["3037"], "sentiment": 1, "event": "沒有來源", "news_ids": [99]},           # 來源不存在
    ]}
    monkeypatch.setattr(news, "get_client", lambda: FakeClient([res]))
    out = news.analyze_sentiment(items, {"3037": "欣興"})
    assert len(out["events"]) == 1 and out["events"][0]["multi_source"]
    assert out["by_code"]["3037"]["score"] == 2.0
