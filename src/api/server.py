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
from src.api import auth, chat_agent, data, notifier, scheduler, sync
from src.config import CACHE_DIR, DATA_DIR, FACTS_DIR, REPORT_DIR, ROOT
from src.utils import json_safe

log = logging.getLogger("api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_cloud = sync.cloud()


class SafeJSONResponse(JSONResponse):
    """任何 API 回應裡的 NaN／Infinity 都轉成 null，避免單一缺值讓整頁 500（10/06 盤前報告的美股快照就是這樣壞的）。"""

    def render(self, content) -> bytes:
        try:
            return super().render(content)
        except ValueError:
            return super().render(json_safe(content))


app = FastAPI(title="投資競賽助手", default_response_class=SafeJSONResponse, docs_url=None if _cloud else "/api/docs", openapi_url=None if _cloud else "/api/openapi.json")
for _d in (DATA_DIR, CACHE_DIR, FACTS_DIR, REPORT_DIR):  # 公開 Space 不含資料夾，啟動時建立
    _d.mkdir(parents=True, exist_ok=True)
_LOGIN_FAILS: dict[str, list[float]] = {}
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
WARM = {"ready": False, "started": time.time(), "error": None}
api = Depends(auth.require_auth)


def _warm():
    try:
        sync.pull()
        on_demand.context()  # 預載全市場日 K 與當日模型
        WARM["ready"] = True
        data.live_alerts(refresh=True)  # 預先計算警示（含新聞），期間的請求先拿報告內的警示
        data.live_intraday(refresh=True)  # 預先計算盤中／建倉建議（期間的請求先拿雲端存檔）
        data.track_record()
    except Exception as e:  # noqa: BLE001
        WARM["error"] = str(e)[:200]
        log.exception("預載失敗")


@app.on_event("startup")
def startup():
    sync.start_background()
    if sync.cloud():
        scheduler.start_background()
        notifier.start_background()
    threading.Thread(target=_warm, daemon=True).start()


# ---------------------------------------------------------------- 登入與狀態
class Login(BaseModel):
    password: str


@app.post("/api/login")
def login(body: Login, response: Response, request: Request):
    if not auth.auth_required():
        return {"ok": True}
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "?")
    now = time.time()
    fails = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < 600]
    if len(fails) >= 5:  # 10 分鐘內錯 5 次就暫停，防止猜密碼
        raise HTTPException(429, "密碼錯誤次數過多，請 10 分鐘後再試")
    if not auth.check_password(body.password):
        _LOGIN_FAILS[ip] = fails + [now]
        raise HTTPException(401, "密碼錯誤")
    _LOGIN_FAILS.pop(ip, None)
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


# ---------------------------------------------------------------- 交易紀錄：手動新增／修改／刪除與統計
class TradeIn(BaseModel):
    date: str
    code: str
    side: str
    shares: float
    price: float
    margin: bool = False
    note: str = ""


class TradeEdit(BaseModel):
    version: str
    trade: TradeIn | None = None


def _trade_op(op: str, version: str, trade: TradeIn | None = None, tid: int | None = None):
    try:
        return data.trade_mutate(op, version, trade.model_dump() if trade else None, tid)
    except sync.ConflictError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/trades", dependencies=[api])
def trades_():
    return data.trade_ledger()


@app.post("/api/trades", dependencies=[api])
def trade_add(body: TradeEdit):
    if not body.trade:
        raise HTTPException(400, "缺少交易內容")
    return _trade_op("add", body.version, body.trade)


@app.put("/api/trades/{tid}", dependencies=[api])
def trade_update(tid: int, body: TradeEdit):
    if not body.trade:
        raise HTTPException(400, "缺少交易內容")
    return _trade_op("update", body.version, body.trade, tid)


@app.delete("/api/trades/{tid}", dependencies=[api])
def trade_delete(tid: int, version: str):
    return _trade_op("delete", version, None, tid)


@app.get("/api/quotes", dependencies=[api])
def quotes(codes: str = ""):
    return data.live_quotes(codes.split(","))


@app.get("/api/intl", dependencies=[api])
def intl(codes: str = ""):
    return data.intl_impacts(codes.split(","))


@app.get("/api/news/live", dependencies=[api])
def news_live(refresh: bool = False):
    return data.live_news(refresh)


@app.get("/api/holding-advice", dependencies=[api])
def holding_advice_(refresh: bool = False):
    return on_demand.portfolio_plan(refresh)


@app.get("/api/holding-watch", dependencies=[api])
def holding_watch_():
    return data.holding_watch()


@app.get("/api/intraday", dependencies=[api])
def intraday_(refresh: bool = False):
    return data.live_intraday(refresh)


class WeeklyReq(BaseModel):
    week: str | None = None


@app.post("/api/weekly-report", dependencies=[api])
def weekly_report(body: WeeklyReq):
    try:
        return data.weekly_report(body.week)
    except Exception as e:  # noqa: BLE001
        log.exception("週報產生失敗")
        raise HTTPException(500, f"週報產生失敗：{str(e)[:200]}") from e


@app.get("/api/weekly-reports", dependencies=[api])
def weekly_reports():
    return {"files": data.weekly_reports(), "weeks": data.competition_weeks()}


@app.get("/api/health", dependencies=[api])
def health_():
    return data.health()


@app.get("/api/data-sources", dependencies=[api])
def data_sources():
    from src.data import providers

    return providers.status()


@app.get("/api/potential", dependencies=[api])
def potential_():
    return data.potential()


@app.get("/api/value-picks", dependencies=[api])
def value_picks_(max_price: float = 50, etf: bool = False):
    return data.value_picks(min(max(max_price, 5), 2000), include_etf=etf)


@app.get("/api/alerts", dependencies=[api])
def alerts_(refresh: bool = False):
    return data.live_alerts(refresh)


@app.get("/api/model", dependencies=[api])
def model():
    return data.model_health()


# ---------------------------------------------------------------- 個股
@app.get("/api/stock/search", dependencies=[api])
def search(q: str):
    return (data.futures_search(q) + on_demand.resolve(q)) if q.strip() else []


@app.get("/api/futures/{root}", dependencies=[api])
def futures_detail(root: str):
    if root.upper() not in data.FUTURES_INFO:
        raise HTTPException(404, "目前支援 TX、MTX、TMF、TXO")
    return data.futures_detail(root)


@app.get("/api/futures/{root}/kline", dependencies=[api])
def futures_kline(root: str, days: int = 260):
    if root.upper() not in data.FUTURES_INFO:
        raise HTTPException(404, "目前支援 TX、MTX、TMF、TXO")
    return data.futures_kline(root.upper(), min(max(days, 30), 500))


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
    msgs = [m.model_dump() for m in body.messages if m.content.strip()][-20:]
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(400, "缺少問題")
    return EventSourceResponse(chat_agent.stream(msgs))


class XQImport(BaseModel):
    text: str
    complete: bool = True


@app.post("/api/xq/import", dependencies=[api])
def xq_import_(body: XQImport):
    return chat_agent.xq_sync_preview(body.text, body.complete)


class XQShot(BaseModel):
    image: str  # data URL 或 base64
    complete: bool = True


@app.post("/api/xq/screenshot", dependencies=[api])
def xq_screenshot_(body: XQShot):
    import base64

    raw = body.image.split(",", 1)[1] if body.image.startswith("data:") else body.image
    if len(raw) > 14_000_000:
        raise HTTPException(413, "圖片太大（上限約 10MB）")
    try:
        img = base64.b64decode(raw)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, "圖片格式錯誤") from e
    return chat_agent.xq_screenshot_preview(img, body.complete)


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
