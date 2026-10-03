"""VerifierAgent：核對 LLM 文字中的每個數字與股票代號都能在事實資料（facts）中找到。純程式，不用 LLM。"""
from __future__ import annotations

import re

NUM_RE = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?")
CODE_RE = re.compile(r"(?<!\d)(\d{4,6}[A-Z]?)(?!\d)")


DATE_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")


def _collect(obj, nums: set[float], texts: list[str], dates: set[float] | None = None) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        nums.add(float(obj))
    elif isinstance(obj, str):
        texts.append(obj)
        if dates is not None:
            for y, mo, d in DATE_RE.findall(obj):  # 日期只接受本身（年、月、日），不做倍數換算
                dates.update({float(y), float(mo), float(d)})
            obj = DATE_RE.sub(" ", obj)
        for m in NUM_RE.findall(obj):
            try:
                nums.add(float(m.replace(",", "")))
            except ValueError:
                pass
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _collect(k, nums, texts, dates)
            _collect(v, nums, texts, dates)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            _collect(v, nums, texts, dates)


def fact_numbers(facts) -> set[float]:
    nums: set[float] = set()
    dates: set[float] = set()
    _collect(facts, nums, [], dates)
    expanded = set(nums) | dates
    for v in nums:  # 允許的換算：比例↔百分比、張↔股、元↔萬元/億元
        a = abs(v)
        expanded.add(a)
        if a < 1.5:
            expanded.add(v * 100)  # 0.616 → 61.6%
        if a >= 1:
            expanded.update({v / 100, v * 1000})  # 61.6% → 0.616；10 張 → 10000 股
        if a >= 1000:
            expanded.update({v / 1000, v / 1e4, v / 1e8})  # 股→張、元→萬元/億元
    return expanded


def _matches(x: float, facts: set[float]) -> bool:
    if float(x).is_integer() and abs(x) <= 3:  # 「第 1 段」「兩三檔」等序數；其餘數字都必須出自事實資料
        return True
    tol = max(0.011, abs(x) * 0.006)
    return any(abs(x - v) <= tol for v in facts)


def verify(text: str, facts) -> tuple[bool, list[str]]:
    nums = fact_numbers(facts)
    bad = []
    for m in NUM_RE.findall(text):
        try:
            x = float(m.replace(",", ""))
        except ValueError:
            continue
        if not _matches(x, nums) and not _matches(abs(x), nums):
            bad.append(m)
    return (not bad), sorted(set(bad))
