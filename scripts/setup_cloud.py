"""一鍵設定雲端：GitHub Secrets／變數、Hugging Face Space Secrets、部署 Space。

需要在 .env 加入（不會被 commit）：
  HF_TOKEN=hf_...     Hugging Face → Settings → Access Tokens → 權限 Write
  GH_PAT=github_pat_... GitHub fine-grained token，只選 repo xq，權限：
                        Contents、Actions、Secrets、Variables 皆為 Read and write
可選：APP_PASSWORD（網頁登入密碼；未填則自動產生並寫回 .env）

執行：.venv\\Scripts\\python scripts\\setup_cloud.py
金鑰只會送到 GitHub／Hugging Face 官方 API，畫面上不會顯示金鑰內容。
"""
from __future__ import annotations

import base64
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import config  # noqa: E402,F401  載入 .env

GH_REPO = os.getenv("GITHUB_REPO_NAME", "ZHANGFU-XIANG/xq")
HF_SPACE = os.getenv("HF_SPACE", "avad8/xq")
GH_API = "https://api.github.com"
SHARED = ["GEMINI_API_KEY", "GROQ_API_KEY", "FINMIND_TOKEN"]
GH_ONLY = ["SMTP_USER", "SMTP_APP_PASSWORD", "REPORT_TO"]


def step(msg: str) -> None:
    print(f"\n▶ {msg}", flush=True)


def need(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        sys.exit(f"✗ .env 缺少 {name}，請參考本檔開頭說明加入後再執行")
    return v


def ensure_env(name: str, gen) -> str:
    v = os.getenv(name, "").strip()
    if v:
        return v
    v = gen()
    with (ROOT / ".env").open("a", encoding="utf-8") as f:
        f.write(f"\n{name}={v}\n")
    os.environ[name] = v
    return v


def gh(method: str, path: str, pat: str, **kw):
    r = requests.request(method, GH_API + path, headers={"Authorization": f"Bearer {pat}",
                                                         "Accept": "application/vnd.github+json"}, timeout=30, **kw)
    if r.status_code >= 400:
        sys.exit(f"✗ GitHub API {method} {path} 失敗（{r.status_code}）：{r.text[:200]}\n"
                 f"  請確認 GH_PAT 只選了 {GH_REPO}，且 Contents/Actions/Secrets/Variables 都是 Read and write")
    return r


def github_secrets(pat: str, hf_token: str) -> None:
    from nacl import encoding, public

    step(f"設定 GitHub Secrets（{GH_REPO}）")
    key = gh("GET", f"/repos/{GH_REPO}/actions/secrets/public-key", pat).json()
    box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder()))
    values = {k: os.getenv(k, "") for k in SHARED + GH_ONLY}
    values["HF_TOKEN"] = hf_token
    for name, val in values.items():
        if not val:
            print(f"  － 略過 {name}（.env 沒有值）")
            continue
        enc = base64.b64encode(box.encrypt(val.encode())).decode()
        gh("PUT", f"/repos/{GH_REPO}/actions/secrets/{name}", pat, json={"encrypted_value": enc, "key_id": key["key_id"]})
        print(f"  ✓ {name}")
    step("設定 GitHub 變數 HF_SPACE")
    r = requests.get(f"{GH_API}/repos/{GH_REPO}/actions/variables/HF_SPACE",
                     headers={"Authorization": f"Bearer {pat}"}, timeout=30)
    if r.status_code == 200:
        gh("PATCH", f"/repos/{GH_REPO}/actions/variables/HF_SPACE", pat, json={"name": "HF_SPACE", "value": HF_SPACE})
    else:
        gh("POST", f"/repos/{GH_REPO}/actions/variables", pat, json={"name": "HF_SPACE", "value": HF_SPACE})
    print(f"  ✓ HF_SPACE = {HF_SPACE}")


def space_secrets(api, pat: str, app_pw: str, jwt: str) -> None:
    step(f"設定 Hugging Face Space Secrets（{HF_SPACE}）")
    values = {k: os.getenv(k, "") for k in SHARED}
    values.update({"APP_PASSWORD": app_pw, "JWT_SECRET": jwt, "GITHUB_REPO": GH_REPO, "GITHUB_TOKEN": pat,
                   "APP_MODE": "cloud"})
    for name, val in values.items():
        if val:
            api.add_space_secret(HF_SPACE, name, val)
            print(f"  ✓ {name}")


def deploy(api) -> None:
    step("建置前端（npm run build）")
    npm = "npm.cmd" if os.name == "nt" else "npm"
    subprocess.run([npm, "ci"], cwd=ROOT / "web", check=True)
    subprocess.run([npm, "run", "build"], cwd=ROOT / "web", check=True)
    step("上傳到 Space")
    readme = ("---\ntitle: 投資擂台研究台\nemoji: 📈\ncolorFrom: indigo\ncolorTo: blue\nsdk: gradio\n"
              "sdk_version: 6.29.1\npython_version: \"3.12\"\napp_file: app.py\npinned: false\n"
              "startup_duration_timeout: 1h\n---\n2026 德明X富達投資擂台 隊伍研究台（需密碼）。\n")
    ignore = [".venv/**", ".git/**", ".env", "data/**", "config/learned_params.yaml", "reports/**", "web/node_modules/**",
              "**/__pycache__/**", ".pytest_cache/**", "Dockerfile", ".dockerignore", "README.md", "scripts/**"]
    api.upload_folder(folder_path=str(ROOT), repo_id=HF_SPACE, repo_type="space", ignore_patterns=ignore,
                      commit_message="deploy from local setup_cloud.py")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "README.md"
        p.write_text(readme, encoding="utf-8")
        api.upload_file(path_or_fileobj=str(p), path_in_repo="README.md", repo_id=HF_SPACE, repo_type="space",
                        commit_message="space config")
    print("  ✓ 已上傳")


def wait_running(api) -> str:
    step("等待 Space 建置與啟動（約 3～10 分鐘）")
    last = None
    for _ in range(120):
        rt = api.get_space_runtime(HF_SPACE)
        if rt.stage != last:
            print(f"  狀態：{rt.stage}", flush=True)
            last = rt.stage
        if rt.stage == "RUNNING":
            return "RUNNING"
        if rt.stage in ("BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR"):
            return rt.stage
        time.sleep(10)
    return last or "UNKNOWN"


def main() -> None:
    from huggingface_hub import HfApi

    hf_token, pat = need("HF_TOKEN"), need("GH_PAT")
    app_pw = ensure_env("APP_PASSWORD", lambda: secrets.token_urlsafe(9))
    jwt = ensure_env("JWT_SECRET", lambda: secrets.token_urlsafe(40))

    step("驗證權杖")
    api = HfApi(token=hf_token)
    who = api.whoami()["name"]
    print(f"  ✓ Hugging Face 帳號：{who}")
    repo = gh("GET", f"/repos/{GH_REPO}", pat).json()
    print(f"  ✓ GitHub repo：{repo['full_name']}（{'private' if repo['private'] else 'public'}）")

    github_secrets(pat, hf_token)
    space_secrets(api, pat, app_pw, jwt)
    deploy(api)
    stage = wait_running(api)
    host = HF_SPACE.replace("/", "-").lower()
    print(f"\n結果：{stage}")
    print(f"網址：https://{host}.hf.space   （Space 頁面：https://huggingface.co/spaces/{HF_SPACE}）")
    print("登入密碼：.env 的 APP_PASSWORD")
    if stage != "RUNNING":
        print(f"請查看 Logs：https://huggingface.co/spaces/{HF_SPACE}?logs=container")


if __name__ == "__main__":
    main()
