"""OpenAI 相容 API（NVIDIA build.nvidia.com、其他開源模型供應商）的極簡 HTTP 用戶端，不需額外套件。"""
from __future__ import annotations

import json
import re

import requests

PROVIDERS = {
    "nvidia": {"base_url": "https://integrate.api.nvidia.com/v1", "key_env": "NVIDIA_API_KEY"},
}


class APIError(RuntimeError):
    def __init__(self, status: int, msg: str):
        super().__init__(f"{status} {msg}")
        self.status = status


def chat(base_url: str, key: str, model: str, messages: list[dict], *, tools: list[dict] | None = None,
         temperature: float = 0, max_tokens: int = 4096, json_mode: bool = False, timeout: int = 120,
         thinking: bool | None = None) -> dict:
    """回傳 choices[0].message（dict：content、tool_calls）。
    thinking=False 關閉推理模型的思考（Nemotron 用 enable_thinking、DeepSeek 用 thinking，其他模型忽略）；
    JSON 輸出一定要關：思考會先把 max_tokens 用完，content 變成空的。"""
    body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if thinking is False:
        body["chat_template_kwargs"] = {"enable_thinking": False, "thinking": False}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = requests.post(f"{base_url}/chat/completions", json=body, timeout=timeout,
                      headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    if r.status_code >= 400:
        raise APIError(r.status_code, r.text[:300])
    choice = r.json()["choices"][0]
    msg = choice["message"]
    if not (msg.get("content") or msg.get("tool_calls")) and choice.get("finish_reason") == "length":
        raise APIError(200, f"輸出長度用完仍沒有回答（max_tokens={max_tokens}，多半是思考過長）")
    return {"content": msg.get("content") or "", "tool_calls": msg.get("tool_calls") or []}


def extract_json(text: str) -> str:
    """模型偶爾在 JSON 前後加說明或 ```json 區塊；取出第一個完整的 JSON 物件。"""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)  # 推理模型的思考內容
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        return m.group(1)
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        candidate = text[i:j + 1]
        json.loads(candidate)  # 不是合法 JSON 就丟錯，讓呼叫端換模型
        return candidate
    return text
