"""FilingAgent：讀取法說會簡報／財報 PDF（Gemini 原生 PDF 理解），輸出附頁碼的重點。

PDF 來源：data/pdfs/{代號}_任意名稱.pdf，或 settings.yaml filings.urls。
摘要結果依檔案雜湊快取，同一份 PDF 只會呼叫一次 LLM（節省免費額度）。
"""
from __future__ import annotations

import hashlib
import json
import logging

import requests
from pydantic import BaseModel, Field

from src.config import CACHE_DIR, PDF_DIR, settings
from src.llm.client import LLMError, get_client

log = logging.getLogger(__name__)


class FilingPoint(BaseModel):
    point: str = Field(description="重點，數字必須照抄 PDF 原文")
    page: int = Field(description="出處頁碼")
    sentiment: int = Field(ge=-2, le=2)


class FilingSummary(BaseModel):
    company: str
    period: str = Field(description="涵蓋期間，例如 2026Q3")
    points: list[FilingPoint] = Field(description="最多 8 點：營收、毛利率、展望、資本支出、產能等")


SYSTEM = "你是財報分析師。只能根據 PDF 內容回答，每一點標註頁碼，數字照抄原文，PDF 沒寫的不要寫。"


def _download_urls() -> None:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    for item in settings().get("filings", {}).get("urls", []) or []:
        dest = PDF_DIR / f"{item['code']}_{hashlib.md5(item['url'].encode()).hexdigest()[:8]}.pdf"
        if dest.exists():
            continue
        try:
            r = requests.get(item["url"], timeout=60)
            r.raise_for_status()
            dest.write_bytes(r.content)
        except Exception as e:  # noqa: BLE001
            log.warning("下載 PDF 失敗 %s: %s", item["url"], e)


def summarize_filings() -> dict[str, list[dict]]:
    _download_urls()
    client = get_client()
    out: dict[str, list[dict]] = {}
    for pdf in sorted(PDF_DIR.glob("*.pdf")):
        code = pdf.stem.split("_")[0]
        digest = hashlib.sha1(pdf.read_bytes()).hexdigest()[:16]
        cache = CACHE_DIR / f"filing_{digest}.json"
        if cache.exists():
            summary = json.loads(cache.read_text(encoding="utf-8"))
        elif client.available():
            try:
                res = client.structured("請整理這份法說會/財報 PDF 的投資重點。", FilingSummary, system=SYSTEM, pdf=pdf)
            except LLMError as e:
                log.warning("PDF 摘要失敗 %s: %s", pdf.name, e)
                continue
            summary = {**res.model_dump(), "file": pdf.name}
            cache.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
        else:
            continue
        out.setdefault(code, []).append(summary)
    return out
