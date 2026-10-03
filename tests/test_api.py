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
    assert new.endswith("2026-10-05,2892,BUY,10000,38.5,0,x\n") and calls["body"]["sha"] == "abc"


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
