"""Hugging Face Spaces（Gradio SDK）進入點：Space 會執行 `python app.py`，這裡直接在 7860 埠啟動 FastAPI（含 React 前端）。"""
import os

import uvicorn

os.environ.setdefault("APP_MODE", "cloud")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# 免費帳號的 Gradio Space 預設為 ZeroGPU 硬體，啟動時必須回報至少一個 @spaces.GPU 函式，否則會被關閉。
# 平常這個回報是在 gradio 的 launch() 裡觸發；我們用 uvicorn 直接啟動 FastAPI，所以自己呼叫 startup()。
# 本系統全部在 CPU 上執行，這個函式只為通過檢查，不會被呼叫。
try:
    import spaces

    @spaces.GPU
    def _zero_gpu_placeholder():
        return None

    from spaces import zero

    if hasattr(zero, "startup"):
        zero.startup()
except ImportError:  # 本機或非 ZeroGPU 環境
    pass

if __name__ == "__main__":
    uvicorn.run("src.api.server:app", host="0.0.0.0", port=int(os.getenv("PORT", "7860")))
