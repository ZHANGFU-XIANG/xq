"""API 與 AI 助理測試（不連網）：登入、檔案路徑防護、工具迴圈數字核對、寫入動作必須確認。"""
import time

import pytest
from fastapi.testclient import TestClient

from src.api import chat_agent, server, sync


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "_warm", lambda: None)
    monkeypatch.setattr(sync, "start_background", lambda *a, **k: None)
    with TestClient(server.app) as c:
        yield c


def test_login_required_when_password_set(client, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "team-secret")
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    assert client.get("/api/reports").status_code == 401
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.get("/api/status").json()["authenticated"] is False
    r = client.post("/api/login", json={"password": "team-secret"})
    assert r.status_code == 200
    assert client.get("/api/reports").status_code == 200
    assert client.get("/api/status").json()["authenticated"] is True


def test_open_when_no_password(client, monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    assert client.get("/api/reports").status_code == 200


def test_file_download_blocks_path_traversal(client, monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    for path in ("/api/files/../config/settings.yaml", "/api/files/..%2F..%2F.env", "/..%2F..%2F.env",
                 "/..%2F..%2Fconfig%2Fsettings.yaml", "/api/nope"):
        r = client.get(path)
        assert "GEMINI" not in r.text and "gemini_models" not in r.text, path


class _Avail:
    def available(self):
        return True


def _fake_loop(answer, tool_calls):
    def loop(messages, emit):
        outs = []
        for name, args in tool_calls:
            emit({"type": "tool", "name": name, "args": args})
            outs.append({"tool": name, "args": args, "result": chat_agent.call_tool(name, args)})
        return answer, outs
    return loop


def test_chat_marks_numbers_not_from_tools(monkeypatch):
    monkeypatch.setattr(chat_agent, "get_client", lambda: _Avail())
    monkeypatch.setattr(chat_agent, "call_tool", lambda n, a: {"close": 240.0, "p_up": 0.616})
    loop = _fake_loop("長榮收盤 240.0 元，收正機率 61.6%，目標價 300 元", [("stock_profile", {"code": "2603"})])
    ev = []
    res = chat_agent.run([{"role": "user", "content": "長榮？"}], ev.append, loop=loop)
    assert not res["verified"] and "300" in res["unverified"]
    assert "300〔未核實〕" in res["text"] and "240.0〔" not in res["text"]
    assert ev[0]["type"] == "tool"


def test_chat_numbers_from_user_message_are_allowed(monkeypatch):
    monkeypatch.setattr(chat_agent, "get_client", lambda: _Avail())
    loop = _fake_loop("好的，記錄 2892 共 10000 股、價格 38.5，請按確認。",
                      [("record_trade", {"code": "2892", "side": "BUY", "shares": 10000, "price": 38.5})])
    res = chat_agent.run([{"role": "user", "content": "我買了 2892 第一金 10 張 38.5"}], lambda e: None, loop=loop)
    assert res["verified"]
    assert len(res["actions"]) == 1


def test_record_trade_requires_confirmation(monkeypatch):
    written = []
    monkeypatch.setattr(sync, "record_trade", lambda row: written.append(row) or "ok")
    out = chat_agent.call_tool("record_trade", {"code": "2892", "side": "BUY", "shares": 10000, "price": 38.5})
    assert out["pending_action"] and not written  # 呼叫工具不會直接寫入
    res = chat_agent.confirm(out["pending_action"])
    assert res["ok"] and written[0]["code"] == "2892" and written[0]["shares"] == 10000
    assert not chat_agent.confirm(out["pending_action"])["ok"]  # 不能重複執行


def test_expired_or_cancelled_actions_do_nothing(monkeypatch):
    written = []
    monkeypatch.setattr(sync, "record_trade", lambda row: written.append(row) or "ok")
    a = chat_agent.call_tool("record_trade", {"code": "2892", "side": "BUY", "shares": 1000, "price": 38.5})["pending_action"]
    chat_agent.PENDING[a]["created"] = time.time() - 4000
    assert not chat_agent.confirm(a)["ok"]
    b = chat_agent.call_tool("record_trade", {"code": "2892", "side": "BUY", "shares": 1000, "price": 38.5})["pending_action"]
    chat_agent.cancel(b)
    assert not chat_agent.confirm(b)["ok"] and not written


def test_warrant_cannot_be_recorded():
    assert "error" in chat_agent.call_tool("record_trade", {"code": "030123", "side": "BUY", "shares": 1000, "price": 1})


def test_rate_limit():
    sid = f"t{time.time()}"
    assert all(chat_agent.rate_ok(sid, per_min=3) for _ in range(3))
    assert not chat_agent.rate_ok(sid, per_min=3)


class _Resp:
    def __init__(self, data=None):
        self._d = data or {}

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


def test_cloud_record_trade_commits_to_github(monkeypatch, tmp_path):
    import base64

    from src.state import portfolio as pfm

    monkeypatch.setenv("APP_MODE", "cloud")
    monkeypatch.setenv("GITHUB_REPO", "team/invest")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setattr(pfm, "TRADES_CSV", tmp_path / "trades.csv")
    existing = "date,code,side,shares,price,margin,note\n"
    calls = {}
    monkeypatch.setattr(sync.requests, "get", lambda url, **k: _Resp({"content": base64.b64encode(existing.encode()).decode(), "sha": "abc"}))
    monkeypatch.setattr(sync.requests, "put", lambda url, **k: calls.update(url=url, body=k["json"]) or _Resp())
    row = {"date": "2026-10-05", "code": "2892", "side": "BUY", "shares": 10000, "price": 38.5, "margin": 0, "note": "x"}
    msg = sync.record_trade(row)
    assert "GitHub" in msg and calls["url"].endswith("/repos/team/invest/contents/data/trades.csv")
    new = base64.b64decode(calls["body"]["content"]).decode()
    assert new.endswith("2026-10-05,2892,BUY,10000,38.5,0,x,,\n") and calls["body"]["sha"] == "abc"
    assert new.startswith("date,code,side,shares,price,margin,note,asset,contract\n")


def test_cloud_record_rejects_overselling(monkeypatch, tmp_path):
    from src.state import portfolio as pfm

    monkeypatch.setattr(pfm, "TRADES_CSV", tmp_path / "trades.csv")
    with pytest.raises(ValueError):
        sync.record_trade({"date": "2026-10-05", "code": "2892", "side": "SELL", "shares": 1000, "price": 38.5, "margin": 0})


def test_cloud_run_workflow_dispatch(monkeypatch):
    monkeypatch.setenv("APP_MODE", "cloud")
    monkeypatch.setenv("GITHUB_REPO", "team/invest")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    calls = {}
    monkeypatch.setattr(sync.requests, "post", lambda url, **k: calls.update(url=url, body=k["json"]) or _Resp())
    sync.run_workflow("postclose")
    assert calls["url"].endswith("/actions/workflows/invest-agent.yml/dispatches")
    assert calls["body"]["inputs"] == {"mode": "postclose", "force": "true"}


def test_header_token_works_when_cookie_blocked(client, monkeypatch):
    """Space 在 huggingface.co 的 iframe 內，第三方 cookie 常被擋：改用標頭也要能登入。"""
    monkeypatch.setenv("APP_PASSWORD", "pw")
    monkeypatch.setenv("JWT_SECRET", "y" * 32)
    token = client.post("/api/login", json={"password": "pw"}).json()["token"]
    client.cookies.clear()
    assert client.get("/api/reports").status_code == 401
    assert client.get("/api/reports", headers={"X-Session-Token": token}).status_code == 200
    assert client.get("/api/status", headers={"X-Session-Token": token}).json()["authenticated"] is True
    # 壞掉的舊 cookie 不應蓋過有效標頭
    client.cookies.set("invest_session", "garbage")
    assert client.get("/api/reports", headers={"X-Session-Token": token}).status_code == 200
    # 網址參數只對檔案下載有效
    assert client.get(f"/api/reports?t={token}").status_code == 401


def test_cloud_cookie_is_iframe_compatible(client, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "pw")
    monkeypatch.setattr(sync, "cloud", lambda: True)
    r = client.post("/api/login", json={"password": "pw"})
    sc = r.headers["set-cookie"].lower()
    assert "samesite=none" in sc and "secure" in sc and "partitioned" in sc and "httponly" in sc


def test_xq_sync_requires_confirmation_and_sells_first(monkeypatch, tmp_path):
    from src.state import portfolio as pfm

    monkeypatch.setattr(pfm, "TRADES_CSV", tmp_path / "t.csv")
    pfm.append_trades([{"date": "2026-10-05", "code": "3037", "side": "BUY", "shares": 1000, "price": 1300, "margin": 0}])
    written = []
    monkeypatch.setattr(sync, "record_trades", lambda rows: written.extend(rows) or "ok")  # 多筆合併成一次寫入
    out = chat_agent.xq_sync_preview("代碼\t名稱\t庫存股數\t成本均價\n2330\t台積電\t1000\t2450\n")
    assert out["pending_action"] and not written
    assert chat_agent.confirm(out["pending_action"])["ok"]
    assert [w["side"] for w in written] == ["SELL", "BUY"] and written[0]["code"] == "3037"


def test_xq_sync_rejects_unparseable_text():
    assert "error" in chat_agent.xq_sync_preview("隨便一段文字，沒有代碼")


def test_login_rate_limited_after_five_failures(client, monkeypatch):
    from src.api import server

    monkeypatch.setenv("APP_PASSWORD", "right")
    server._LOGIN_FAILS.clear()
    for _ in range(5):
        assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"password": "right"}).status_code == 429  # 鎖定期間連正確密碼也擋
    server._LOGIN_FAILS.clear()
    assert client.post("/api/login", json={"password": "right"}).status_code == 200


def test_potential_excludes_orders(client, monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    from src.agents import on_demand

    # 乾淨環境（GitHub Actions 部署前測試）沒有日 K 快取，context() 會下載全市場 10 年資料（實測卡 250 秒）；ETF 候補段落本來就會略過失敗
    monkeypatch.setattr(on_demand, "context", lambda: (_ for _ in ()).throw(RuntimeError("測試不下載市場資料")))
    r = client.get("/api/potential")
    assert r.status_code == 200
    d = r.json()
    if d["date"]:
        from src.api import data

        ordered = {o["code"] for o in data.load_report()["orders"]}
        assert not ordered & {x["code"] for x in d["offense"] + d["defense"]}
        assert all(x["why_not_yet"] and x["trigger"] for x in d["offense"] + d["defense"])


def test_answer_style_and_citations():
    t = chat_agent._style("好的，以下是分析：\n### 結論\n**聯發科** 排第 33 名〔S2〕 🚀\n---\n建議：觀望")
    assert "**" not in t and "#" not in t and "---" not in t and "🚀" not in t and not t.startswith("好的")
    txt, bad = chat_agent._check_cites("A〔S1〕B〔S9〕", 2)
    assert bad == ["S9"] and "〔出處不明〕" in txt and "〔S1〕" in txt
    hidden = chat_agent._hide_cites("排名 33〔S12〕")
    assert "12" not in hidden and chat_agent._show_cites(hidden) == "排名 33〔S12〕"


def test_citation_numbers_not_flagged_and_sources_listed(monkeypatch):
    # 不依賴真的 API 金鑰（GitHub Actions 部署前測試沒有 .env）：迴圈已用假的取代，只要讓「已設定 LLM」檢查通過
    monkeypatch.setattr(chat_agent, "get_client", lambda: type("C", (), {"available": lambda self: True})())

    def fake(messages, emit, start=0):
        out = chat_agent._tag({"price": 48.85}, start + 1, "stock_profile")
        return "兆豐金收盤 48.85 元〔S1〕。", [{"tool": "stock_profile", "args": {"code": "2886"}, "result": out}]

    r = chat_agent.run([{"role": "user", "content": "兆豐金多少錢"}], lambda e: None, loop=fake)
    assert r["verified"] and r["sources"][0]["id"] == "S1" and r["sources"][0]["cited"]


def test_uncited_points_flagged():
    t, n = chat_agent._flag_uncited("結論。\n1. 收盤 48.85〔S1〕\n2. 其他來源也沒有公布\n推論：可能是假期\n3. 推論：市場觀望\n建議：觀望")
    assert n == 1 and "2. 其他來源也沒有公布〔未附出處〕" in t and "〔S1〕〔未附出處〕" not in t


def test_list_ordinals_not_flagged():
    t = chat_agent._hide_cites("結論\n6. 大台 48671 點〔S2〕\n7、保證金")
    assert "6" not in t.split("大台")[0] and chat_agent._show_cites(t) == "結論\n6. 大台 48671 點〔S2〕\n7、保證金"


def test_live_intraday_serves_saved_while_recomputing(monkeypatch, tmp_path):
    import threading
    import time as _t

    from src.agents import intraday
    from src.api import data

    started = threading.Event()

    def slow_run(report):
        started.set()
        _t.sleep(0.3)
        return {"time": "now", "orders": [], "slot": "x"}

    monkeypatch.setattr(intraday, "run", slow_run)
    monkeypatch.setattr(intraday, "current_slot", lambda now=None: "2026-10-05 09:40")
    monkeypatch.setattr(data, "_saved_intraday", lambda: {"time": "2026-10-05 09:00", "orders": [{"code": "2330"}]})
    monkeypatch.setattr(data, "_INTRA", {"slot": None, "key": None, "v": None})
    monkeypatch.setattr(data, "load_report", lambda key=None: {})
    t0 = _t.time()
    v = data.live_intraday()
    assert v["stale"] and v["cached_from"] == "2026-10-05 09:00" and _t.time() - t0 < 0.2  # 不等重算
    assert started.wait(2)
    _t.sleep(0.5)
    assert data.live_intraday()["time"] == "now"  # 背景算完後換成新的


def test_exec_step_dedupes_and_keeps_source_ids(monkeypatch):
    """同一步的多個查詢平行執行；同一輪重複的查詢沿用原本的結果與出處編號，不重算。"""
    calls = []
    monkeypatch.setattr(chat_agent, "call_tool", lambda n, a: calls.append((n, a)) or {"v": len(calls)})
    chat_agent._TL.cache = {}
    try:
        outputs, seen = [], {}
        outs, rep = chat_agent._exec_step([("stock_profile", {"code": "2330"}), ("get_regime", {})], lambda e: None, outputs, 0, seen)
        assert not rep and [o["source_id"] for o in outs] == ["S1", "S2"]
        outs, rep = chat_agent._exec_step([("stock_profile", {"code": "2330"})], lambda e: None, outputs, 0, seen)
        assert rep and outs[0]["source_id"] == "S1" and len(outputs) == 2 and len(calls) == 2
    finally:
        chat_agent._TL.cache = None


def test_nvidia_loop_has_no_step_cap_but_stops_repeating(monkeypatch):
    """工具次數不設上限（10 個不同查詢都照跑）；一直重複同樣查詢時改成不給工具、要求直接回答。"""
    from src.llm import openai_compat as oc

    monkeypatch.setenv("NVIDIA_API_KEY", "x")
    monkeypatch.setattr(chat_agent, "settings", lambda: {"llm": {"nvidia_models": ["m"]}})
    monkeypatch.setattr(chat_agent, "model_ok", lambda m: True)
    monkeypatch.setattr(chat_agent, "_system", lambda: "sys")
    monkeypatch.setattr(chat_agent, "call_tool", lambda n, a: {"ok": a.get("code")})
    seq = [str(i) for i in range(10)] + ["9", "9"]
    no_tools = []

    def fake_chat(base, key, model, msgs, tools=None, **kw):
        if tools is None:
            no_tools.append(True)
            return {"content": "答案", "tool_calls": []}
        code = seq.pop(0) if seq else "9"
        return {"content": "", "tool_calls": [{"id": code, "function": {"name": "stock_profile", "arguments": f'{{"code": "{code}"}}'}}]}

    monkeypatch.setattr(oc, "chat", fake_chat)
    text, outputs = chat_agent._nvidia_loop([{"role": "user", "content": "q"}], lambda e: None)
    assert text == "答案" and len(outputs) == 10 and no_tools


def test_board_detail_lines_are_appended():
    out = {"tool": "get_decision_board", "args": {}, "result": {"source_id": "S1", "buy": [{"line": "A（1）可以買：限價 10 買 1 張"}],
                                                                   "wait": [], "avoid": [], "hold": [{"line": "B（2）續抱：理由"}]}}
    s = chat_agent._board_detail([out])
    assert "手上持股（1 檔）\n1. B（2）續抱：理由〔S1〕" in s and "可以買（1 檔）" in s and "等回檔" not in s


def test_decision_word():
    from src.agents.on_demand import _decision

    assert _decision("建議買進（穩健籃）", False) == "可以買"
    assert _decision("過熱不追：RSI 90", False) == "等回檔再買"
    assert _decision("觀望：兩籃分數都未達", False) == "先不要買"
    assert _decision("停損換股：跌破", True) == "全部出清"
    assert _decision("續抱：分數仍高", True) == "續抱"


def test_uncited_judgement_becomes_inference():
    t, n = chat_agent._flag_uncited("結論。\n1. 未依此減碼則可能面臨成交困難，建議執行減碼\n2. 其他來源也沒有公布")
    assert n == 1 and "1. 推論：未依此減碼" in t and "2. 其他來源也沒有公布〔未附出處〕" in t
