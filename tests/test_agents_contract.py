import time

from src.agents import agents_catalog, briefing, calc, fact_check, registry


def test_every_agent_registered_with_contract():
    registry._load_all()
    assert set(agents_catalog.ALL) <= set(registry.REGISTRY)
    assert set(agents_catalog.FAST) <= set(agents_catalog.ALL) and "debate" not in agents_catalog.FAST
    for n, s in registry.REGISTRY.items():
        assert s.title and s.timeout > 0 and s.required, n
        assert "error" in s.fallback("x")


def test_registry_degrades_on_error_and_timeout(monkeypatch):
    registry._load_all()
    monkeypatch.setitem(registry.REGISTRY, "boom", registry.AgentSpec("boom", "壞掉", lambda **k: 1 / 0, 5, False, ("x",)))
    monkeypatch.setitem(registry.REGISTRY, "slow", registry.AgentSpec("slow", "很慢", lambda **k: time.sleep(3) or {"x": 1}, 1, False, ("x",)))
    monkeypatch.setitem(registry.REGISTRY, "good", registry.AgentSpec("good", "正常", lambda **k: {"x": 1}, 5, False, ("x",)))
    monkeypatch.setitem(registry.REGISTRY, "bad", registry.AgentSpec("bad", "缺欄位", lambda **k: {"y": 1}, 5, False, ("x",)))
    r = registry.run(["boom", "slow", "good", "bad"])
    assert r["good"]["ok"] and not r["boom"]["ok"] and "error" in r["boom"]["output"]
    assert not r["slow"]["ok"] and "逾時" in r["slow"]["error"]
    assert not r["bad"]["ok"] and "缺欄位 x" in r["bad"]["error"]


def test_briefing_builds_from_partial_results():
    res = {"risk": {"ok": True, "output": {"stress": [{"scenario": "加權指數 −5%", "total": -100, "pct_nav": -1.0, "futures": -50}],
                                            "alerts": [{"level": "嚴重", "title": "期貨距追繳只剩 2.5%", "body": "x"}]}},
           "forum": {"ok": False, "output": {"error": "逾時"}, "error": "逾時"}}
    b = briefing.build(res)
    assert b["sections"][0]["title"] == "風險" and b["sections"][0]["level"] == "嚴重"
    assert b["failed"] and "期貨距追繳" in b["text"]


def test_fact_check_tiers_and_hype():
    items = fact_check.score([
        {"title": "台積電法說會上修全年營收 - 經濟日報", "source": "Google新聞"},
        {"title": "台積電法說會上修全年營收展望 - 工商時報", "source": "Google新聞"},
        {"title": "這檔必漲飆漲穩賺 老師報牌", "source": "某部落格"},
        {"title": "某公司澄清 傳聞不實", "source": "公開資訊觀測站"}])
    assert items[0]["fc_label"] == "已證實" and "2 個來源報導" in items[0]["fc_notes"]
    assert items[2]["fc_label"] == "未證實"
    assert items[3]["clarified"]


def test_calc_matches_xq_settlement():
    c = calc.calculate("cost", code="2915", side="SELL", shares=172, price=58.6)
    assert c["net_cash"] == 10030  # XQ 成交檔 10/12 潤泰全零股淨收付
    assert calc.calculate("breakeven", code="2330", shares=1000, buy_price=1000)["breakeven_pct"] > 0.58
