"""以 Gmail SMTP 寄送報告（需環境變數 SMTP_USER、SMTP_APP_PASSWORD、REPORT_TO）。

網站（Hugging Face Space，APP_MODE=cloud）不能對外連 SMTP（只開放 80/443，直接連會 Network is unreachable），
改觸發 GitHub Actions 的 send-email.yml 代寄；收件人與帳密用 GitHub Secrets。附件只能由 GitHub Actions 端寄送。
"""
from __future__ import annotations

import logging
import os
import smtplib

import requests
from email.message import EmailMessage
from pathlib import Path

from src.config import settings

log = logging.getLogger(__name__)


def html_body(subject: str, body: str) -> str | None:
    """scripts/mail_html.py（代寄端也用同一份）把純文字轉成 HTML 版面；失敗就只寄純文字。"""
    try:
        import importlib.util

        path = Path(__file__).resolve().parents[2] / "scripts" / "mail_html.py"
        spec = importlib.util.spec_from_file_location("mail_html", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.render(subject, body)
    except Exception as e:  # noqa: BLE001
        log.warning("HTML 信件版面失敗，改寄純文字：%s", e)
        return None


def _via_github(subject: str, body: str) -> bool:
    repo, tok = os.environ["GITHUB_REPO"], os.environ["GITHUB_TOKEN"]
    if len(body) > 20000:  # workflow_dispatch 輸入有長度上限
        body = body[:20000] + "\n…（內容過長已截斷，詳情請開網站）"
    try:
        r = requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/send-email.yml/dispatches",
                          headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
                          json={"ref": os.getenv("GITHUB_BRANCH", "main"), "inputs": {"subject": subject[:200], "body": body}},
                          timeout=30)
        r.raise_for_status()
        log.info("已交由 GitHub Actions 代寄：%s", subject)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("GitHub 代寄失敗：%s", e)
        return False


def send(subject: str, body: str, attachments: list[Path]) -> bool:
    if not settings()["email"]["enabled"]:
        return False
    if os.getenv("APP_MODE") == "cloud" and os.getenv("GITHUB_REPO") and os.getenv("GITHUB_TOKEN"):
        if attachments:
            log.warning("網站代寄不含附件（%d 個）", len(attachments))
        return _via_github(subject, body)
    user, pwd = os.getenv("SMTP_USER"), os.getenv("SMTP_APP_PASSWORD")
    to = [t.strip() for t in (os.getenv("REPORT_TO") or ",".join(settings()["email"]["to"] or [])).split(",") if t.strip()]
    if not settings()["email"]["enabled"] or not (user and pwd and to):
        log.info("未設定寄信參數，略過 Email")
        return False
    if os.getenv("DRY_RUN"):  # 雲端測試模式：資料不會提交，Email 照寄但標示為測試
        subject = "[測試] " + subject
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(to)
    msg.set_content(body)
    h = html_body(subject, body)
    if h:
        msg.add_alternative(h, subtype="html")
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
