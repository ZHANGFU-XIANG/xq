"""綜合排名的消息可信度規則（防假消息、傳聞）。"""
from src.api.data import _news_credibility


def test_official_or_multi_source_full_weight():
    assert _news_credibility([{"title": "公告本公司受邀參加法人說明會", "source": "公開資訊觀測站"}])[0] == 1.0
    assert _news_credibility([{"title": "營收創新高", "source": "鉅亨網"}, {"title": "營收創新高", "source": "經濟日報"}])[0] == 1.0


def test_single_source_or_none_discounted():
    assert _news_credibility([{"title": "傳接大單", "source": "某論壇"}])[0] == 0.5
    assert _news_credibility([])[0] == 0.3


def test_clarification_overrides():
    score, note = _news_credibility([{"title": "大立光電澄清媒體報導", "source": "公開資訊觀測站"},
                                     {"title": "毛利率新低", "source": "鉅亨網"}])
    assert score == 0.3 and "澄清" in note
