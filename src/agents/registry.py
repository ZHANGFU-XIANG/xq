"""AgentRegistry：所有輔助 Agent 在這裡登記，主流程透過 run() 平行呼叫（逾時、失敗降級、記錄狀態）。

為什麼：主流程（main.run_daily、網站即時查詢）原本直接呼叫十幾個模組，一個失敗或變慢就拖垮整份報告。
每個 Agent 宣告：名稱、中文說明、函式、逾時秒數、是否用 LLM、必要輸出欄位（contract test 檢查）。
run() 一律回傳 {name: {"ok", "seconds", "output", "error"}}，失敗的 output 是 fallback（空結果＋原因），主流程照常產出。
/api/agents 顯示每個 Agent 最近一次的狀態與耗時。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
from dataclasses import dataclass, field
from typing import Callable

from src.config import now_tw

log = logging.getLogger(__name__)


@dataclass
class AgentSpec:
    name: str
    title: str
    fn: Callable[..., dict]
    timeout: float = 60
    uses_llm: bool = False
    required: tuple[str, ...] = ()
    fallback: Callable[[str], dict] = field(default=lambda err: {"error": err})


REGISTRY: dict[str, AgentSpec] = {}
STATUS: dict[str, dict] = {}


def register(name: str, title: str, *, timeout: float = 60, uses_llm: bool = False, required: tuple[str, ...] = (),
             fallback: Callable[[str], dict] | None = None):
    def deco(fn):
        REGISTRY[name] = AgentSpec(name, title, fn, timeout, uses_llm, required, fallback or (lambda err: {"error": err}))
        return fn

    return deco


def validate(name: str, out: dict) -> list[str]:
    spec = REGISTRY[name]
    if not isinstance(out, dict):
        return ["輸出不是 dict"]
    return [f"缺欄位 {k}" for k in spec.required if k not in out]


def _load_all() -> None:
    """匯入各 Agent 模組（模組載入時會呼叫 register）。"""
    from src.agents import agents_catalog  # noqa: F401


def run(names: list[str] | None = None, max_workers: int = 6, **kwargs) -> dict[str, dict]:
    _load_all()
    names = names or list(REGISTRY)
    out: dict[str, dict] = {}
    ex = ThreadPoolExecutor(max_workers=max_workers)
    futs = {n: (ex.submit(REGISTRY[n].fn, **kwargs), time.time()) for n in names if n in REGISTRY}
    for n, (f, t0) in futs.items():
        spec = REGISTRY[n]
        try:
            res = f.result(timeout=max(1.0, spec.timeout - (time.time() - t0)))
            bad = validate(n, res)
            ok, err = not bad, "；".join(bad) or None
        except FTimeout:
            res, ok, err = spec.fallback(f"逾時 {spec.timeout:.0f} 秒"), False, f"逾時 {spec.timeout:.0f} 秒"
        except Exception as e:  # noqa: BLE001
            log.warning("Agent %s 失敗：%s", n, e)
            res, ok, err = spec.fallback(str(e)[:160]), False, str(e)[:160]
        secs = round(time.time() - t0, 1)
        out[n] = {"ok": ok, "seconds": secs, "output": res, "error": err}
        STATUS[n] = {"name": n, "title": spec.title, "ok": ok, "seconds": secs, "error": err, "uses_llm": spec.uses_llm,
                     "time": now_tw().strftime("%m/%d %H:%M:%S")}
    ex.shutdown(wait=False, cancel_futures=True)
    return out


def status() -> list[dict]:
    _load_all()
    return [STATUS.get(n) or {"name": n, "title": s.title, "ok": None, "uses_llm": s.uses_llm, "time": None}
            for n, s in REGISTRY.items()]
