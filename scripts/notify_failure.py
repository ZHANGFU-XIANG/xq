"""GitHub Actions 排程失敗通知：只用標準函式庫（套件安裝失敗時也能寄出）。

用法：python scripts/notify_failure.py "invest-agent" "premarket"
需要環境變數 SMTP_USER、SMTP_APP_PASSWORD、REPORT_TO；GitHub 會自動提供 GITHUB_* 變數。
"""
import os
import smtplib
import sys
from email.message import EmailMessage


def main() -> int:
    workflow = sys.argv[1] if len(sys.argv) > 1 else os.getenv("GITHUB_WORKFLOW", "workflow")
    detail = sys.argv[2] if len(sys.argv) > 2 else ""
    user, pwd = os.getenv("SMTP_USER"), os.getenv("SMTP_APP_PASSWORD")
    to = [t.strip() for t in (os.getenv("REPORT_TO") or "").split(",") if t.strip()]
    if not (user and pwd and to):
        print("未設定 SMTP，無法寄出失敗通知")
        return 0
    url = f"{os.getenv('GITHUB_SERVER_URL', 'https://github.com')}/{os.getenv('GITHUB_REPOSITORY', '')}/actions/runs/{os.getenv('GITHUB_RUN_ID', '')}"
    msg = EmailMessage()
    msg["Subject"] = f"[投資競賽] 排程失敗：{workflow}" + (f"／{detail}" if detail else "")
    msg["From"], msg["To"] = user, ", ".join(to)
    msg.set_content(
        f"雲端排程「{workflow}{'／' + detail if detail else ''}」執行失敗。\n\n"
        f"執行紀錄（可看錯誤訊息）：{url}\n\n"
        "網站上的建議可能沒有更新；可以到 GitHub Actions 頁面按「Re-run jobs」重跑，"
        "或在網站 AI 助理說「重新執行盤前報告」。")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(user, pwd)
        s.send_message(msg)
    print("已寄出失敗通知")
    return 0


if __name__ == "__main__":
    sys.exit(main())
