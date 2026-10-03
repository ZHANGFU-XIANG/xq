"""LLM 用戶端：Gemini 免費層為主、Groq 為備援。

設計原則（防幻覺）：
- temperature 固定 0
- 一律要求 JSON 結構化輸出，並用 pydantic 驗證；驗證失敗視同呼叫失敗
- 沒有任何 API key 時 available() 回傳 False，呼叫端必須能退回純程式／模板邏輯
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from src.config import settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


_COOLDOWN: dict[str, float] = {}  # 額度用盡（429）的模型暫停使用 1 小時


def model_ok(model: str) -> bool:
    return time.time() >= _COOLDOWN.get(model, 0)


def mark_exhausted(model: str, msg: str) -> None:
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        _COOLDOWN[model] = time.time() + 3600


class LLMClient:
    def __init__(self) -> None:
        cfg = settings()["llm"]
        self.cfg = cfg
        self._last_call = 0.0
        self._gemini = None
        self._groq = None
        if os.getenv("GEMINI_API_KEY"):
            from google import genai

            self._gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        if os.getenv("GROQ_API_KEY"):
            from groq import Groq

            self._groq = Groq(api_key=os.environ["GROQ_API_KEY"])
        self.calls = 0

    def available(self) -> bool:
        return self._gemini is not None or self._groq is not None

    def _throttle(self) -> None:
        wait = self.cfg["min_interval_sec"] - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def structured(self, prompt: str, schema: type[T], *, system: str = "", pdf: Path | None = None) -> T:
        """回傳經 pydantic 驗證的結構化結果；全部供應商失敗時丟 LLMError。"""
        errors: list[str] = []
        if self._gemini is not None:
            for model in [m for m in self.cfg["gemini_models"] if model_ok(m)]:
                for attempt in range(self.cfg["max_retries"]):
                    try:
                        return self._gemini_call(model, prompt, schema, system, pdf)
                    except ValidationError as e:
                        errors.append(f"{model}: 格式驗證失敗 {e.error_count()} 項")
                    except Exception as e:  # noqa: BLE001 - 供應商錯誤種類繁多
                        msg = str(e)
                        errors.append(f"{model}: {msg[:160]}")
                        mark_exhausted(model, msg)
                        if model not in [m for m in self.cfg["gemini_models"] if model_ok(m)]:
                            break
                        if "404" in msg or "not found" in msg.lower():
                            break  # 模型不存在，換下一個
                        if "503" in msg or "UNAVAILABLE" in msg:
                            break  # 模型過載，直接換下一個
                        time.sleep(min(60, 8 * (attempt + 1)))
        if self._groq is not None and pdf is None:
            for model in self.cfg["groq_models"]:
                for attempt in range(self.cfg["max_retries"]):
                    try:
                        return self._groq_call(model, prompt, schema, system)
                    except ValidationError as e:
                        errors.append(f"{model}: 格式驗證失敗 {e.error_count()} 項")
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"{model}: {str(e)[:160]}")
                        time.sleep(5 * (attempt + 1))
        raise LLMError("; ".join(errors) or "未設定 GEMINI_API_KEY / GROQ_API_KEY")

    def _gemini_call(self, model: str, prompt: str, schema: type[T], system: str, pdf: Path | None) -> T:
        from google.genai import types

        self._throttle()
        self.calls += 1
        contents: list = []
        if pdf is not None:
            contents.append(types.Part.from_bytes(data=pdf.read_bytes(), mime_type="application/pdf"))
        contents.append(prompt)
        resp = self._gemini.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=self.cfg["temperature"],
                system_instruction=system or None,
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
        return schema.model_validate_json(resp.text)

    def _groq_call(self, model: str, prompt: str, schema: type[T], system: str) -> T:
        self._throttle()
        self.calls += 1
        sys_msg = (system + "\n\n" if system else "") + (
            "只輸出 JSON，必須符合此 JSON Schema：\n" + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        )
        resp = self._groq.chat.completions.create(
            model=model,
            temperature=self.cfg["temperature"],
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": sys_msg}, {"role": "user", "content": prompt}],
        )
        return schema.model_validate_json(resp.choices[0].message.content)


_client: LLMClient | None = None


def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
