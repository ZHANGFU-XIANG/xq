"""StandingAgent：追蹤競賽排名。使用者上傳每週排名截圖 → 視覺模型讀出名次與報酬 → 程式算出差距。

- 讀取：沿用 xq_vision 的 Gemini（優先）／NVIDIA 視覺模型，輸出固定 JSON 結構；讀不到的欄位留空，不猜
- 紀錄：data/standing.json（雲端寫回 GitHub），每次上傳一筆
- 計算：距前 20 名（決賽簡報資格）差幾個百分點、距正報酬（才列入排名）差多少元，用目前淨值換算
"""
from __future__ import annotations

import base64
import json
import logging
import os

from pydantic import BaseModel, Field

from src.config import DATA_DIR, now_tw, rules, settings

log = logging.getLogger(__name__)
PATH = DATA_DIR / "standing.json"
PROMPT = ("這是投資競賽的排名畫面截圖。請讀出：我方隊伍名次（若畫面有標示本隊）、我方報酬率、參賽隊數、"
          "第 20 名的報酬率、第 1 名報酬率、排名更新日期。看不到或不確定的欄位填 null，不要猜。百分比只填數字（例如 3.25）。")


class StandingRead(BaseModel):
    my_rank: int | None = Field(default=None)
    my_return_pct: float | None = Field(default=None)
    total_teams: int | None = Field(default=None)
    rank20_return_pct: float | None = Field(default=None)
    top_return_pct: float | None = Field(default=None)
    as_of: str | None = Field(default=None, description="排名更新日期，YYYY-MM-DD")


def read(img: bytes) -> tuple[StandingRead, str]:
    from src.agents import xq_vision

    data, mime = xq_vision._shrink(img)
    errors = []
    for m in settings()["llm"].get("gemini_vision_models", []) if os.getenv("GEMINI_API_KEY") else []:
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
            r = client.models.generate_content(
                model=m, contents=[types.Part.from_bytes(data=data, mime_type=mime), PROMPT],
                config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json",
                                                   response_schema=StandingRead))
            return StandingRead.model_validate_json(r.text), m
        except Exception as e:  # noqa: BLE001
            errors.append(f"{m}: {str(e)[:100]}")
    for m in settings()["llm"].get("nvidia_vision_models", []) if os.getenv("NVIDIA_API_KEY") else []:
        try:
            from src.llm import openai_compat as oc

            b64 = base64.b64encode(data).decode()
            msg = oc.chat(oc.PROVIDERS["nvidia"]["base_url"], os.environ["NVIDIA_API_KEY"], m,
                          [{"role": "user", "content": [{"type": "text", "text": PROMPT + "\n只輸出 JSON。"},
                                                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}],
                          temperature=0, max_tokens=1024, thinking=False, timeout=60)
            return StandingRead.model_validate_json(oc.extract_json(msg["content"])), m
        except Exception as e:  # noqa: BLE001
            errors.append(f"{m}: {str(e)[:100]}")
    raise RuntimeError("；".join(errors) or "未設定可讀圖片的模型")


def history() -> list[dict]:
    return json.loads(PATH.read_text(encoding="utf-8")) if PATH.exists() else []


def save(rec: dict) -> str:
    from src.api import sync

    rows = history() + [rec]
    return sync.put_file("data/standing.json", json.dumps(rows, ensure_ascii=False, indent=1),
                         f"standing: {rec.get('as_of') or rec['uploaded']} 第 {rec.get('my_rank')} 名")


def summary() -> dict:
    """最新一筆排名＋目前淨值換算的差距。"""
    from src.state import portfolio as pfm

    rows = history()
    hist = pfm.load_history()
    nav = hist[-1]["nav"] if hist else None
    initial = rules()["capital"]["initial"]
    ret_now = (nav / initial - 1) * 100 if nav else None
    last = rows[-1] if rows else None
    out = {"latest": last, "history": rows[-12:], "return_now_pct": round(ret_now, 3) if ret_now is not None else None,
           "to_positive_amount": round(max(0.0, initial - nav)) if nav else None}
    if last and last.get("rank20_return_pct") is not None and ret_now is not None:
        gap = last["rank20_return_pct"] - ret_now
        out["gap_to_top20_pct"] = round(gap, 2)
        out["gap_to_top20_amount"] = round(gap / 100 * initial)
    out["notes"] = [x for x in [
        f"距正報酬還差 {out['to_positive_amount']:,} 元（報酬為正才列入排名）" if out.get("to_positive_amount") else None,
        (f"距第 20 名（決賽簡報資格）差 {out['gap_to_top20_pct']:+.2f} 個百分點（約 {out['gap_to_top20_amount']:,} 元）"
         if out.get("gap_to_top20_pct") is not None else "還沒有排名資料：每週五排名公布後，上傳排名截圖即可追蹤")] if x]
    return out


def ingest(img: bytes) -> dict:
    r, model = read(img)
    rec = {**r.model_dump(), "uploaded": now_tw().strftime("%Y-%m-%d %H:%M"), "model": model}
    msg = save(rec)
    return {"record": rec, "message": msg, "summary": summary()}
