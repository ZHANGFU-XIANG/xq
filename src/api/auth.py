"""共用密碼登入：APP_PASSWORD 驗證後發 HttpOnly cookie（JWT，30 天）。本機模式未設密碼時不需登入。"""
from __future__ import annotations

import hmac
import os
import time

import jwt
from fastapi import HTTPException, Request

COOKIE = "invest_session"
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


def session_id(request: Request) -> str:
    """登入者識別（用於速率限制）；未啟用登入時以 IP 區分。"""
    return request.cookies.get(COOKIE, "")[-16:] or (request.client.host if request.client else "local")


def require_auth(request: Request) -> None:
    if not auth_required():
        return
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "請先登入")
    try:
        jwt.decode(token, _secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "登入已過期，請重新登入") from None
