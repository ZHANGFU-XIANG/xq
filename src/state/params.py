"""可調參數：settings.yaml 為預設值，config/learned_params.yaml 為調整 Agent 寫入的覆寫值。

各 Agent 一律透過這裡讀取策略／模型參數，調整 Agent 修改後下次執行即生效。
競賽規則（rules.yaml）永遠不在可調範圍內。
"""
from __future__ import annotations

import copy

import yaml

from src.config import CONFIG_DIR, settings

LEARNED = CONFIG_DIR / "learned_params.yaml"
SECTIONS = ("strategy", "score_weights", "alpha", "predictor", "allocation", "baskets", "defense_weights")


def learned() -> dict:
    if not LEARNED.exists():
        return {}
    return yaml.safe_load(LEARNED.read_text(encoding="utf-8")) or {}


def save_learned(data: dict) -> None:
    LEARNED.write_text("# 由調整 Agent 自動產生；刪除此檔即回到 settings.yaml 預設值\n"
                       + yaml.safe_dump(data, allow_unicode=True, sort_keys=True), encoding="utf-8")


def get(section: str) -> dict:
    base = copy.deepcopy(settings().get(section, {}))
    base.update(learned().get(section, {}))
    return base


def basket(name: str) -> dict:
    return get("baskets")[name]


def allocation() -> dict:
    return get("allocation")


def strategy() -> dict:
    return get("strategy")


def score_weights() -> dict:
    w = get("score_weights")
    total = sum(w.values()) or 1.0
    return {k: v / total for k, v in w.items()}


def alpha() -> dict:
    return get("alpha")


def predictor() -> dict:
    return get("predictor")
