"""Yahoo Finance 統一存取層：所有 yfinance 呼叫都經過這裡。

2026-10-14 全歷史下載與盤後同時跑時遇到 YFRateLimitError（Too Many Requests），各模組各自呼叫、各自失敗，
結果是某些股票靜默缺資料。這一層負責：
- 同一個程序內限流：兩次請求至少間隔 MIN_GAP 秒（多執行緒共用一把鎖）
- 被限流或空結果時指數退避重試（2、5、12 秒）；download 只重抓缺的代號
- 成功／失敗記到 providers（模型頁「資料來源狀態」看得到）
"""
from __future__ import annotations

import logging
import threading
import time

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)
MIN_GAP = 0.35
BACKOFF = (2, 5, 12)
_lock = threading.Lock()
_last = [0.0]
SOURCE = "Yahoo Finance"


def _wait() -> None:
    with _lock:
        gap = time.time() - _last[0]
        if gap < MIN_GAP:
            time.sleep(MIN_GAP - gap)
        _last[0] = time.time()


def _mark(ok: bool, msg: str = "") -> None:
    try:
        from src.data import providers

        providers._mark(SOURCE, ok, msg)
    except Exception:  # noqa: BLE001
        pass


def _limited(e: Exception) -> bool:
    s = f"{type(e).__name__} {e}"
    return "RateLimit" in s or "Too Many Requests" in s or "429" in s


def history(symbol: str, **kw) -> pd.DataFrame:
    """yf.Ticker(symbol).history(**kw)，限流＋退避重試；全部失敗回傳空表。"""
    for i, wait in enumerate((0, *BACKOFF)):
        if wait:
            time.sleep(wait)
        _wait()
        try:
            df = yf.Ticker(symbol).history(**kw)
            if not df.empty:
                _mark(True)
                return df
        except Exception as e:  # noqa: BLE001
            if not _limited(e) or i == len(BACKOFF):
                _mark(False, f"{symbol}：{str(e)[:80]}")
                log.warning("Yahoo %s 失敗：%s", symbol, e)
                return pd.DataFrame()
            log.info("Yahoo 限流，%d 秒後重試 %s", BACKOFF[i], symbol)
    _mark(False, f"{symbol}：無資料")
    return pd.DataFrame()


def _fields(d: pd.DataFrame, sym: str, multi: bool) -> pd.DataFrame | None:
    try:
        x = d[sym] if multi else d
    except KeyError:
        return None
    x = x.dropna(how="all")
    return x if not x.empty else None


def download(symbols: list[str], **kw) -> dict[str, pd.DataFrame]:
    """批次下載（group_by=ticker），回傳 {代號: DataFrame}；缺的代號退避後只重抓缺的部分。"""
    kw = {"group_by": "ticker", "threads": True, "progress": False, "auto_adjust": False, **kw}
    out: dict[str, pd.DataFrame] = {}
    todo = list(dict.fromkeys(symbols))
    for i, wait in enumerate((0, *BACKOFF)):
        if not todo:
            break
        if wait:
            log.info("Yahoo 批次缺 %d 檔，%d 秒後重抓", len(todo), wait)
            time.sleep(wait)
        _wait()
        try:
            d = yf.download(todo, **kw)
        except Exception as e:  # noqa: BLE001
            if not _limited(e):
                log.warning("Yahoo 批次下載失敗：%s", e)
                break
            continue
        if d is None or d.empty:
            continue
        multi = isinstance(d.columns, pd.MultiIndex)
        for s in todo:
            x = _fields(d, s, multi or len(todo) > 1)
            if x is not None:
                out[s] = x
        missing = [s for s in todo if s not in out]
        # 只有少數缺（多半是下市或代號錯）就不再重試；大量缺才像被限流
        if len(missing) <= max(2, len(todo) * 0.05):
            break
        todo = missing
    _mark(bool(out), "" if out else "批次下載全部失敗")
    return out
