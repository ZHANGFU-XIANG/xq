"""FastAPI 伺服器：/api/* 與 React 靜態網頁（同一個網址）。

本機：  .venv\\Scripts\\python -m uvicorn src.api.server:app --port 8000
雲端：  Dockerfile（Hugging Face Spaces，port 7860，APP_MODE=cloud）
"""
from __future__ import annotations

import logging
import re
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
        WARM["seconds"] = round(time.time() - WARM["started"])  # 10 年資料後的預載耗時（/api/status 可看）
        log.info("預載完成：%d 秒", WARM["seconds"])
        data.live_alerts(refresh=True)  # 預先計算警示（含新聞），期間的請求先拿報告內的警示
        data.live_intraday(refresh=True)  # 預先計算盤中／建倉建議（期間的請求先拿雲端存檔）
        data.track_record()
        from src.agents import daytrade

        daytrade.daily()  # 預先計算當沖日線分數與回測
        from src.agents import agents_catalog, risk_lab
        from src.api import agents_bg

        risk_lab.snapshot()  # 風險儀表板（黑天鵝 26 年回測第一次約 2 分鐘）
        agents_bg.refresh(agents_catalog.FAST)  # 總報告先有快速版，完整版（含 LLM）由背景每小時更新
    except Exception as e:  # noqa: BLE001
        WARM["error"] = str(e)[:200]
        log.exception("預載失敗")


def _rewarm():
    WARM.update(error=None, started=time.time())
    _warm()


@app.on_event("startup")
def startup():
    sync.start_background()
    if sync.cloud():
        scheduler.start_background()
        notifier.start_background()
        from src.api import sre

        sre.start_background(WARM, _rewarm)  # EngineerAgent：每 5 分鐘自我檢查、安全的自動修復、異常寄信
        from src.api import agents_bg

        agents_bg.start_background()  # 輔助 Agent 每小時更新、06:40／21:30 寄總報告
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
            "warm_error": WARM["error"], "warm_seconds": WARM.get("seconds"), "mode": "cloud" if sync.cloud() else "local", "sync": sync.state,
            "target_date": on_demand.current_target().isoformat(), "build": build_id()}


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
    kind: str = "STOCK"  # STOCK／ETF／FUT／OPT／TRANSFER（資金調撥）／CASH（手續費校正）
    date: str
    code: str = ""
    side: str = ""
    shares: float = 1
    price: float
    margin: bool = False
    contract: str = ""
    note: str = ""


class TradeEdit(BaseModel):
    version: str
    trade: TradeIn | None = None


def _trade_op(op: str, version: str, trade: TradeIn | None = None, tid: int | None = None, confirm: str | None = None):
    try:
        return data.trade_mutate(op, version, trade.model_dump() if trade else None, tid, confirm=confirm)
    except sync.ConflictError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


class TradeBulk(BaseModel):
    version: str
    trades: list[TradeIn]


class TradeBulkDelete(BaseModel):
    version: str
    ids: list[int]
    confirm: str | None = None  # 會清掉持股或刪掉一半以上股票紀錄時，必須是「確認刪除」


class TradeRestore(BaseModel):
    version: str
    sha: str


@app.post("/api/trades/bulk", dependencies=[api])
def trade_bulk_add(body: TradeBulk):
    try:
        return data.trade_mutate("bulk_add", body.version, trades=[t.model_dump() for t in body.trades])
    except sync.ConflictError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/trades/bulk-delete", dependencies=[api])
def trade_bulk_delete(body: TradeBulkDelete):
    try:
        return data.trade_mutate("bulk_delete", body.version, ids=body.ids, confirm=body.confirm)
    except sync.ConflictError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


class CsvFile(BaseModel):
    data: str | None = None  # base64（瀏覽器讀檔，XQ 匯出為 Big5）
    files: list[str] = []    # 一次多個檔案（成交回報＋委託回報：委託回報用來對出成交日期）


@app.post("/api/xq/csv", dependencies=[api])
def xq_csv_(body: CsvFile):
    """XQ 匯出的委託／成交回報 CSV → 成交草稿（標示已記錄過的），使用者核對後用 /api/trades/bulk 寫入。"""
    import base64

    from src.agents import xq_csv
    from src.state import portfolio as pfm

    blobs = [x for x in ([body.data] if body.data else []) + body.files if x]
    if not blobs:
        raise HTTPException(400, "沒有檔案")
    if sum(len(x) for x in blobs) > 6_000_000:
        raise HTTPException(413, "檔案太大（合計上限約 4MB）")
    try:
        raws = [base64.b64decode(x.split(",", 1)[1] if x.startswith("data:") else x) for x in blobs]
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, "檔案格式錯誤") from e
    out = xq_csv.parse_many(raws)
    if out.get("rows") is not None:
        xq_csv.mark_duplicates(out["rows"])
    out["version"] = pfm.trades_version()
    return out


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
def trade_delete(tid: int, version: str, confirm: str | None = None):
    return _trade_op("delete", version, None, tid, confirm)


@app.get("/api/trades/history", dependencies=[api])
def trade_history():
    """交易紀錄的歷史版本（誤刪時用來復原）。"""
    try:
        return data.trades_history()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"讀取歷史版本失敗：{str(e)[:120]}") from e


@app.post("/api/trades/restore", dependencies=[api])
def trade_restore(body: TradeRestore):
    try:
        return data.trade_restore(body.sha, body.version)
    except sync.ConflictError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


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


@app.get("/api/regime", dependencies=[api])
def regime_():
    return data.live_regime()


@app.get("/api/holding-watch", dependencies=[api])
def holding_watch_():
    return data.holding_watch()


@app.get("/api/presentation", dependencies=[api])
def presentation_(rebuild: bool = False):
    from src.agents import presentation

    if rebuild:
        presentation.build()
    return presentation.latest()


@app.get("/api/anomaly", dependencies=[api])
def anomaly_():
    from src.agents import market_anomaly

    out = dict(market_anomaly.snapshot())
    return out


@app.get("/api/conference", dependencies=[api])
def conference_():
    from src.agents import conference
    from src.api import agents_bg

    return conference.run(agents_bg._picks())


@app.get("/api/fin-reports", dependencies=[api])
def fin_reports_():
    from src.agents import fin_report

    return fin_report.run()


@app.get("/api/shadow", dependencies=[api])
def shadow_():
    from src.agents import shadow

    return shadow.run()


@app.get("/api/execution", dependencies=[api])
def execution_():
    from src.agents import execution

    return execution.run()


@app.get("/api/close-check", dependencies=[api])
def close_check_():
    from src.agents import compliance

    return compliance.close_check()


@app.get("/api/standing", dependencies=[api])
def standing_():
    from src.agents import standing

    return standing.summary()


class ImageBody(BaseModel):
    image: str


@app.post("/api/standing/screenshot", dependencies=[api])
def standing_shot_(body: ImageBody):
    import base64

    from src.agents import standing

    raw = body.image.split(",", 1)[1] if body.image.startswith("data:") else body.image
    if len(raw) > 14_000_000:
        raise HTTPException(413, "圖片太大（上限約 10MB）")
    try:
        return standing.ingest(base64.b64decode(raw))
    except RuntimeError as e:
        raise HTTPException(502, f"讀不出排名：{e}") from e


@app.get("/api/agents", dependencies=[api])
def agents_():
    from src.agents import registry
    from src.api import agents_bg

    return {"agents": registry.status(), "running": agents_bg.STATE["running"], "error": agents_bg.STATE["error"]}


@app.get("/api/briefing", dependencies=[api])
def briefing_(refresh: bool = False):
    """總報告：背景每小時更新；refresh=true 時立即重跑快速 Agent（不含多空辯論與產業新聞 LLM）。"""
    from src.agents import agents_catalog, briefing
    from src.api import agents_bg

    if refresh or briefing.LAST["v"] is None:
        out = agents_bg.refresh(agents_catalog.FAST)
        if briefing.LAST["v"] and not refresh:
            threading.Thread(target=agents_bg.refresh, daemon=True).start()  # 完整版（含 LLM）在背景補上
        return out or briefing.LAST["v"] or {}
    return briefing.LAST["v"]


@app.get("/api/debate", dependencies=[api])
def debate_():
    from src.agents import debate

    return debate.run()


@app.get("/api/sre", dependencies=[api])
def sre_(run: bool = False):
    from src.api import sre

    if run:
        sre.tick(WARM, _rewarm)
    return sre.status()


@app.get("/api/risk", dependencies=[api])
def risk_():
    """風險儀表板：壓力測試、追繳點位、VaR、到競賽結束的模擬、黑天鵝指標、未來 5 個交易日注意事項、資料檢查。"""
    from src.agents import data_guard, risk_lab, trade_calendar
    from src.state import portfolio as pfm

    out = dict(risk_lab.snapshot())
    pf = pfm.build_portfolio()
    try:
        out["calendar"] = trade_calendar.events(list(pf.positions), list(pf.derivs))
    except Exception as e:  # noqa: BLE001
        out["calendar"], out["calendar_error"] = [], str(e)[:120]
    out["data_guard"] = data_guard.STATE.get("last")
    return out


@app.get("/api/overnight", dependencies=[api])
def overnight_():
    from src.agents import overnight

    return overnight.snapshot()


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


@app.get("/api/decisions", dependencies=[api])
def decisions_():
    return data.decision_board()


@app.get("/api/picks", dependencies=[api])
def picks_(limit: int = 20, etf: bool = False, budget: float | None = None, split: int = 1, odd: bool = False,
           max_price: float | None = None):
    return data.high_return_picks(min(max(limit, 5), 50), etf, budget if budget and budget > 0 else None,
                                  min(max(split, 1), 20), odd, max_price if max_price and max_price > 0 else None)


@app.get("/api/events", dependencies=[api])
def events_():
    from src.agents import events

    return events.scan()


@app.get("/api/us-shock", dependencies=[api])
def us_shock_(codes: str = ""):
    from src.agents import us_shock

    return us_shock.view([c for c in codes.split(",") if c] or None)


@app.get("/api/exit-plan/{code}", dependencies=[api])
def exit_plan_(code: str, entry_date: str | None = None, entry_price: float | None = None):
    return data.exit_plan(code, entry_date, entry_price)


@app.get("/api/daytrade", dependencies=[api])
def daytrade_(refresh: bool = False, max_price: float | None = None):
    from src.agents import daytrade

    return daytrade.board(refresh, max_price if max_price and max_price > 0 else None)


@app.get("/api/daytrade/{code}", dependencies=[api])
def daytrade_stock(code: str):
    from src.agents import daytrade

    return daytrade.stock(code)


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
def kline(code: str, days: int = 260, period: str = ""):
    if period:  # 月／週／日／分 K（1、3、5、10、15、30、60）
        try:
            return on_demand.kline_period(code, period)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
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
NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}  # 首頁與 manifest 每次都向伺服器確認，部署後手機不會卡在舊版


def build_id() -> str:
    """目前部署的前端版本（index.html 引用的主程式檔名，內容雜湊）。前端比對不同就自動重新載入。"""
    try:
        m = re.search(r'src="(/assets/index-[^"]+\.js)"', (DIST / "index.html").read_text(encoding="utf-8"))
        return m.group(1) if m else ""
    except OSError:
        return ""


if DIST.exists():
    class _Assets(StaticFiles):
        async def get_response(self, path, scope):
            r = await super().get_response(path, scope)
            if r.status_code == 200:  # 檔名含內容雜湊，可以長期快取
                r.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return r

    app.mount("/assets", _Assets(directory=DIST / "assets"), name="assets")

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404, "找不到 API")
        root = DIST.resolve()
        f = (DIST / full_path).resolve()
        if full_path and root in f.parents and f.is_file():  # 只允許 dist 內的檔案（防路徑穿越）
            return FileResponse(f, headers=NO_CACHE if f.suffix in (".html", ".webmanifest") else None)
        return FileResponse(root / "index.html", headers=NO_CACHE)
