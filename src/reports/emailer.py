"""以 Gmail SMTP 寄送報告（需環境變數 SMTP_USER、SMTP_APP_PASSWORD、REPORT_TO）。"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

from src.config import settings

log = logging.getLogger(__name__)


def send(subject: str, body: str, attachments: list[Path]) -> bool:
    user, pwd = os.getenv("SMTP_USER"), os.getenv("SMTP_APP_PASSWORD")
    to = [t.strip() for t in (os.getenv("REPORT_TO") or ",".join(settings()["email"]["to"] or [])).split(",") if t.strip()]
    if not settings()["email"]["enabled"] or not (user and pwd and to):
        log.info("未設定寄信參數，略過 Email")
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(to)
    msg.set_content(body)
    for p in attachments:
        if p and p.exists():
            msg.add_attachment(p.read_bytes(), maintype="application", subtype="octet-stream", filename=p.name)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
            s.login(user, pwd)
            s.send_message(msg)
        log.info("已寄出報告給 %d 位收件人", len(to))
        return True
    except Exception as e:  # noqa: BLE001
        log.error("寄信失敗：%s", e)
        return False
