"""FastAPI 伺服器：/api/* 與 React 靜態網頁（同一個網址）。

本機：  .venv\\Scripts\\python -m uvicorn src.api.server:app --port 8000
雲端：  Dockerfile（Hugging Face Spaces，port 7860，APP_MODE=cloud）
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from src.agents import on_demand
from src.api import auth, chat_agent, data, sync
from src.config import REPORT_DIR, ROOT, rules

log = logging.getLogger("api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = FastAPI(title="投資競賽助手", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
WARM = {"ready": False, "started": time.time(), "error": None}
api = Depends(auth.require_auth)


def _warm():
    try:
        sync.pull()
        on_demand.context()  # 預載全市場日 K 與當日模型
        WARM["ready"] = True
        data.live_alerts()  # 預先計算警示（含新聞），首次開啟警示頁不必等待
    except Exception as e:  # noqa: BLE001
        WARM["error"] = str(e)[:200]
        log.exception("預載失敗")


@app.on_event("startup")
def startup():
    sync.start_background()
    threading.Thread(target=_warm, daemon=True).start()


# ---------------------------------------------------------------- 登入與狀態
class Login(BaseModel):
    password: str


@app.post("/api/login")
def login(body: Login, response: Response):
    if not auth.auth_required():
        return {"ok": True}
    if not auth.check_password(body.password):
        raise HTTPException(401, "密碼錯誤")
    token = auth.issue_token()
    if sync.cloud():
        # Space 透過 huggingface.co 的 iframe 開啟，cookie 屬第三方，需 SameSite=None＋Partitioned。
        # Starlette 只在 Python 3.14+ 支援 partitioned，Space 是 3.12，因此直接寫標頭（JWT 只含網址安全字元）。
        response.headers.append("set-cookie", f"{auth.COOKIE}={token}; Max-Age={auth.TTL}; Path=/; HttpOnly; "
                                              "Secure; SameSite=None; Partitioned")
    else:
        response.set_cookie(auth.COOKIE, token, max_age=auth.TTL, httponly=True, samesite="lax")
    return {"ok": True, "token": token}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE, samesite="none" if sync.cloud() else "lax", secure=sync.cloud())
    return {"ok": True}


@app.get("/api/status")
def status(request: Request):
    try:
        auth.require_auth(request)
        authed = True
    except HTTPException:
        authed = False
    return {"auth_required": auth.auth_required(), "authenticated": authed, "warm": WARM["ready"],
            "warm_error": WARM["error"], "mode": "cloud" if sync.cloud() else "local", "sync": sync.state,
            "target_date": on_demand.current_target().isoformat()}


# ---------------------------------------------------------------- 報告
@app.get("/api/reports", dependencies=[api])
def reports():
    return data.report_files()


@app.get("/api/report", dependencies=[api])
def report(key: str | None = None):
    r = data.load_report(key)
    if not r:
        raise HTTPException(404, "查無報告")
    return r


@app.get("/api/files/{path:path}", dependencies=[api])
def files(path: str):
    fp = (REPORT_DIR / path).resolve()
    if REPORT_DIR.resolve() not in fp.parents or not fp.is_file():
        raise HTTPException(404, "找不到檔案")
    return FileResponse(fp, filename=fp.name)


@app.get("/api/news", dependencies=[api])
def news(key: str | None = None):
    return data.news(key)


@app.get("/api/portfolio", dependencies=[api])
def portfolio():
    return data.portfolio()


@app.get("/api/holding-advice", dependencies=[api])
def holding_advice_(refresh: bool = False):
    return on_demand.portfolio_plan(refresh)


@app.get("/api/intraday", dependencies=[api])
def intraday_(refresh: bool = False):
    return data.live_intraday(refresh)


@app.get("/api/alerts", dependencies=[api])
def alerts_(refresh: bool = False):
    return data.live_alerts(refresh)


@app.get("/api/model", dependencies=[api])
def model():
    return data.model_health()


@app.get("/api/rules", dependencies=[api])
def get_rules():
    return rules()


# ---------------------------------------------------------------- 個股
@app.get("/api/stock/search", dependencies=[api])
def search(q: str):
    return on_demand.resolve(q) if q.strip() else []


@app.get("/api/stock/{code}/profile", dependencies=[api])
def profile(code: str):
    return on_demand.profile(code)


@app.get("/api/stock/{code}/kline", dependencies=[api])
def kline(code: str, days: int = 260):
    return on_demand.kline(code, min(max(days, 30), 750))


@app.get("/api/stock/{code}/analysis", dependencies=[api])
def analysis(code: str):
    return on_demand.analyze(code)


@app.get("/api/stock/{code}/company", dependencies=[api])
def company(code: str):
    from src.agents import mops

    return mops.company(code)


class MopsNews(BaseModel):
    marketKind: str
    companyId: str
    enterDate: str
    serialNumber: str


@app.post("/api/mops/news", dependencies=[api])
def mops_news(body: MopsNews):
    from src.agents import mops

    return mops.news_detail(body.model_dump())


@app.get("/api/stock/{code}/news", dependencies=[api])
def stock_news(code: str):
    return on_demand.search_news(f"{on_demand.stock_meta(code)['name']} {code}", 15)


# ---------------------------------------------------------------- AI 助理
class ChatMsg(BaseModel):
    role: str
    content: str


class ChatReq(BaseModel):
    messages: list[ChatMsg]


@app.post("/api/chat", dependencies=[api])
def chat(body: ChatReq, request: Request):
    if not chat_agent.rate_ok(auth.session_id(request)):
        raise HTTPException(429, "詢問太頻繁，請稍候一分鐘（保護免費 AI 額度）")
    msgs = [m.model_dump() for m in body.messages if m.content.strip()][-12:]
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(400, "缺少問題")
    return EventSourceResponse(chat_agent.stream(msgs))


class XQImport(BaseModel):
    text: str
    complete: bool = True


@app.post("/api/xq/import", dependencies=[api])
def xq_import_(body: XQImport):
    return chat_agent.xq_sync_preview(body.text, body.complete)


@app.post("/api/actions/{aid}/confirm", dependencies=[api])
def confirm(aid: str):
    try:
        res = chat_agent.confirm(aid)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": f"執行失敗：{str(e)[:200]}"}, status_code=400)
    data._load.cache_clear()
    return res


@app.post("/api/actions/{aid}/cancel", dependencies=[api])
def cancel(aid: str):
    return chat_agent.cancel(aid)


# ---------------------------------------------------------------- 前端靜態檔
DIST = Path(ROOT) / "web" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404, "找不到 API")
        root = DIST.resolve()
        f = (DIST / full_path).resolve()
        if full_path and root in f.parents and f.is_file():  # 只允許 dist 內的檔案（防路徑穿越）
            return FileResponse(f)
        return FileResponse(root / "index.html")
