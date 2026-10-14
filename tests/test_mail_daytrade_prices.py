import importlib.util
from pathlib import Path

from src.agents import daytrade


def _mail():
    spec = importlib.util.spec_from_file_location("mail_html", Path(__file__).parents[1] / "scripts" / "mail_html.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_mail_html_cards_and_colors():
    h = _mail().render("[投資競賽] ［嚴重］夜盤重挫", "■ ［嚴重］台指期夜盤 -2.30%\n估計影響 -300,000 元\n\n現在可以做：\n- 【賣出】3037 欣興\n\n22:10 隔夜即時通知・詳情請開網站")
    assert "<html" in h and "欣興" in h and "#d1242f" in h          # 嚴重＝紅色
    assert "賣出</span>" in h and "-2.30%</span>" in h              # 標籤與漲跌上色
    assert "22:10 隔夜即時通知" in h                                # 結尾說明移到頁首／頁尾，不當卡片


def test_every_row_gets_buy_and_stop():
    d = {"code": "2330", "is_etf": False, "atr": 50.0, "close": 1000.0}
    row = {"verdict": "開盤後觀察", "levels": {"buy_zone": [960.0, 980.0]}}
    p = daytrade._prices(d, row, 10_000_000, 5_000_000)
    assert p["buy_price"] == 980.0 and p["stop_price"] < p["buy_price"] < p["target_price"]
    assert "買入 980.0" in p["price_text"] and "停損" in p["price_text"]
    assert daytrade._prices(d, {"verdict": "準備平倉"}, 1e7, 1e6)["buy_price"] is None
