"""GitHub Actions 代寄 Email（send-email.yml 呼叫）：只用標準函式庫。

需要環境變數 SMTP_USER、SMTP_APP_PASSWORD、REPORT_TO、MAIL_SUBJECT、MAIL_BODY。
"""
import os
import smtplib
import sys
from email.message import EmailMessage


def main() -> int:
    user, pwd = os.getenv("SMTP_USER"), os.getenv("SMTP_APP_PASSWORD")
    to = [t.strip() for t in (os.getenv("REPORT_TO") or "").split(",") if t.strip()]
    if not (user and pwd and to):
        print("未設定 SMTP_USER／SMTP_APP_PASSWORD／REPORT_TO，無法寄出")
        return 1
    msg = EmailMessage()
    msg["Subject"] = os.getenv("MAIL_SUBJECT") or "[投資競賽] 通知"
    msg["From"], msg["To"] = user, ", ".join(to)
    msg.set_content(os.getenv("MAIL_BODY") or "")
    try:  # 好讀的 HTML 版面（同資料夾 mail_html.py）；失敗就只寄純文字
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import mail_html

        msg.add_alternative(mail_html.render(msg["Subject"], os.getenv("MAIL_BODY") or ""), subtype="html")
    except Exception as e:  # noqa: BLE001
        print(f"HTML 版面失敗，只寄純文字：{e}")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(user, pwd)
        s.send_message(msg)
    print(f"已寄出給 {len(to)} 位收件人")
    return 0


if __name__ == "__main__":
    sys.exit(main())
