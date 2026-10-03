"""共用密碼登入：APP_PASSWORD 驗證後發 HttpOnly cookie（JWT，30 天）。本機模式未設密碼時不需登入。"""
from __future__ import annotations

import hmac
import os
import time

import jwt
from fastapi import HTTPException, Request

COOKIE = "invest_session"
HEADER = "X-Session-Token"
TTL = 30 * 86400


def _secret() -> str:
    return os.getenv("JWT_SECRET") or os.getenv("APP_PASSWORD") or "local-dev"


def auth_required() -> bool:
    return bool(os.getenv("APP_PASSWORD"))


def check_password(pw: str) -> bool:
    expected = os.getenv("APP_PASSWORD", "")
    return bool(expected) and hmac.compare_digest(pw.encode(), expected.encode())


def issue_token() -> str:
    now = int(time.time())
    return jwt.encode({"sub": "team", "iat": now, "exp": now + TTL}, _secret(), algorithm="HS256")


def _candidates(request: Request) -> list[str]:
    """cookie、標頭（iframe 內第三方 cookie 被擋時前端改用標頭）、網址參數 t（僅限檔案下載連結）。"""
    c = [request.cookies.get(COOKIE), request.headers.get(HEADER)]
    if request.url.path.startswith("/api/files/"):
        c.append(request.query_params.get("t"))
    return [t for t in c if t]


def _valid(token: str) -> bool:
    try:
        jwt.decode(token, _secret(), algorithms=["HS256"])
        return True
    except jwt.PyJWTError:
        return False


def session_id(request: Request) -> str:
    """登入者識別（用於速率限制）；未啟用登入時以 IP 區分。"""
    c = _candidates(request)
    return (c[0][-16:] if c else "") or (request.client.host if request.client else "local")


def require_auth(request: Request) -> None:
    if not auth_required():
        return
    c = _candidates(request)
    if not c:
        raise HTTPException(401, "請先登入")
    if not any(_valid(t) for t in c):
        raise HTTPException(401, "登入已過期，請重新登入")
