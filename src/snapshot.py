"""市場資料快照：GitHub Actions 算完盤前／盤後後，把網站需要的市場資料與模型結果打包上傳；網站啟動直接下載使用。

原本網站（Hugging Face Space，磁碟不保留）每次重啟或部署都要從 Yahoo 下載 10 年、約 1,000 檔日 K 並重新訓練模型，
預載約 2 分鐘，期間網頁拿不到即時建議，Yahoo 也容易限流。快照內容：
- 全市場日 K 最近 SNAP_DAYS 個交易日（當沖回測、技術特徵、風險計算都夠用）
- 一個月模型結果（alpha_model.run 的輸出，含預測與回測指標）、選股池、ETF 清單、加權指數、美股歷史、DataGuard 結果
存放：私人 repo 的 GitHub Release（tag：market-snapshot），每次覆蓋同名檔案，不讓 git 歷史變大。
"""
from __future__ import annotations

import datetime as dt
import gzip
import logging
import os
import pickle
import time

import requests

from src.config import CACHE_DIR

log = logging.getLogger(__name__)
TAG = "market-snapshot"
ASSET = "market_snapshot.pkl.gz"
SNAP_DAYS = 520
API = "https://api.github.com"


def _repo_token() -> tuple[str, str] | None:
    repo = os.getenv("GITHUB_REPO") or os.getenv("GITHUB_REPOSITORY")
    tok = os.getenv("GITHUB_TOKEN")
    return (repo, tok) if repo and tok else None


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}


def pack(ctx: dict) -> bytes:
    panel = {c: df.tail(SNAP_DAYS) for c, df in ctx["panel"].items()}
    obj = {"version": 1, "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "target": ctx["target"],
           "panel": panel, "codes": ctx["codes"], "etf": ctx["etf"], "taiex": ctx["taiex"], "us_hist": ctx["us_hist"],
           "alpha": ctx["alpha"], "data_guard": ctx.get("data_guard")}
    return gzip.compress(pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL), compresslevel=6)


def publish(ctx: dict) -> bool:
    rt = _repo_token()
    if not rt:
        log.info("沒有 GITHUB_TOKEN，略過上傳快照")
        return False
    repo, tok = rt
    blob = pack(ctx)
    try:
        r = requests.get(f"{API}/repos/{repo}/releases/tags/{TAG}", headers=_h(tok), timeout=30)
        if r.status_code == 404:
            r = requests.post(f"{API}/repos/{repo}/releases", headers=_h(tok), timeout=30,
                              json={"tag_name": TAG, "name": "市場資料快照（自動產生）", "prerelease": True,
                                    "body": "網站啟動用的市場資料快照，每次盤前／盤後自動覆蓋。"})
        r.raise_for_status()
        rel = r.json()
        for a in rel.get("assets", []):
            if a["name"] == ASSET:
                requests.delete(f"{API}/repos/{repo}/releases/assets/{a['id']}", headers=_h(tok), timeout=30)
        up = rel["upload_url"].split("{")[0]
        r = requests.post(f"{up}?name={ASSET}", headers={**_h(tok), "Content-Type": "application/octet-stream"},
                          data=blob, timeout=300)
        r.raise_for_status()
        log.info("已上傳市場快照：目標日 %s，%.1f MB", ctx["target"], len(blob) / 1e6)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("上傳市場快照失敗：%s", e)
        return False


def fetch(target: dt.date, max_age_hours: float = 30) -> dict | None:
    """下載快照（本機也有快取）；目標交易日不符或太舊就回傳 None，由呼叫端照原流程計算。"""
    path = CACHE_DIR / ASSET
    obj = None
    if path.exists() and time.time() - path.stat().st_mtime < 3600:
        obj = pickle.loads(gzip.decompress(path.read_bytes()))
    if obj is None or obj.get("target") != target:
        rt = _repo_token()
        if not rt:
            return None
        repo, tok = rt
        try:
            t0 = time.time()
            rel = requests.get(f"{API}/repos/{repo}/releases/tags/{TAG}", headers=_h(tok), timeout=30)
            if rel.status_code != 200:
                return None
            a = next((a for a in rel.json().get("assets", []) if a["name"] == ASSET), None)
            if not a:
                return None
            r = requests.get(a["url"], headers={**_h(tok), "Accept": "application/octet-stream"}, timeout=300)
            r.raise_for_status()
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_bytes(r.content)
            obj = pickle.loads(gzip.decompress(r.content))
            log.info("已下載市場快照（%.1f MB、%.1f 秒）", len(r.content) / 1e6, time.time() - t0)
        except Exception as e:  # noqa: BLE001
            log.warning("下載市場快照失敗：%s", e)
            return None
    if obj.get("target") != target:
        log.info("快照目標日 %s ≠ %s，改為自行計算", obj.get("target"), target)
        return None
    age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(obj["created"])).total_seconds() / 3600
    if age > max_age_hours:
        return None
    return {"us_hist": obj["us_hist"], "panel": obj["panel"], "codes": obj["codes"], "etf": obj["etf"],
            "taiex": obj["taiex"], "alpha": obj["alpha"], "target": obj["target"], "data_guard": obj.get("data_guard"),
            "from_snapshot": obj["created"]}
