"""Hugging Face Spaces（Gradio SDK）進入點：Space 會執行 `python app.py`，這裡直接在 7860 埠啟動 FastAPI（含 React 前端）。"""
import os

import uvicorn

os.environ.setdefault("APP_MODE", "cloud")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

if __name__ == "__main__":
    uvicorn.run("src.api.server:app", host="0.0.0.0", port=int(os.getenv("PORT", "7860")))
