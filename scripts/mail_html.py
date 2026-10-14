"""把純文字通知轉成好讀的 HTML 信件（只用標準函式庫：GitHub Actions 代寄 send_email.py 也要能用）。

純文字仍一起寄出（multipart/alternative），不支援 HTML 的信箱照樣能讀。
版面規則（依現有報告／盤中建議／即時通知的文字格式）：
- 第一段：第一行是標題；第二行若用「｜」分隔，變成數字小卡
- 以「：」結尾的行、或「■」開頭的行 → 區塊標題；其後「- 」開頭的行 → 條列
- 【買進】【賣出】【加碼】【減碼】等 → 彩色標籤；［嚴重］［注意］［利多］或「嚴重警示」→ 對應顏色的卡片
- +1.23% 紅色、−1.23% 綠色（台股慣例紅漲綠跌）；網址變成連結
"""
from __future__ import annotations

import html
import re

SITE = "https://avad8-xq.hf.space"
C = {"ink": "#1f2328", "muted": "#6b7280", "line": "#e5e7eb", "bg": "#f5f6f8", "card": "#ffffff",
     "up": "#d1242f", "down": "#1a7f37", "warn": "#b45309", "link": "#1f6feb", "head": "#0b2545"}
TAG = {"買進": C["up"], "加碼": C["up"], "做多": C["up"], "賣出": C["down"], "減碼": C["down"], "出清": C["down"],
       "停損": C["down"], "做空": C["down"], "續抱": C["link"], "取消": C["muted"], "預掛": C["link"]}
LEVEL = {"嚴重": (C["up"], "#fff1f0"), "注意": (C["warn"], "#fff8eb"), "利多": (C["down"], "#effaf2")}

_NUM = re.compile(r"(?<![\w.])([+\-−]\d[\d,]*(?:\.\d+)?%)")
_URL = re.compile(r"(https?://[^\s<>()（）]+)")
_TAG = re.compile(r"【([^】]{1,6})】")


def _inline(text: str) -> str:
    t = html.escape(text)
    t = _URL.sub(lambda m: f'<a href="{m.group(1)}" style="color:{C["link"]};text-decoration:none">連結</a>', t)

    def tag(m):
        k = m.group(1)
        col = next((v for key, v in TAG.items() if key in k), C["head"])
        return (f'<span style="display:inline-block;background:{col};color:#fff;border-radius:4px;'
                f'padding:1px 7px;font-size:12px;font-weight:600;margin-right:4px">{k}</span>')

    t = _TAG.sub(tag, t)

    def num(m):
        s = m.group(1)
        col = C["up"] if s.startswith("+") else C["down"]
        return f'<span style="color:{col};font-weight:600">{s}</span>'

    return _NUM.sub(num, t)


def _strip_level(text: str) -> str:
    return re.sub(r"[［\[](嚴重|注意|利多)[］\]]\s*", "", text)


def _badge(level: str | None) -> str:
    if not level:
        return ""
    col = LEVEL[level][0]
    return (f'<span style="display:inline-block;background:{col};color:#fff;border-radius:4px;padding:1px 7px;'
            f'font-size:12px;font-weight:600;margin-right:6px;vertical-align:1px">{level}</span>')


def _level(text: str) -> str | None:
    for k in LEVEL:
        if f"［{k}］" in text or f"[{k}]" in text or text.startswith(f"{k}警示") or text.startswith(f"■ {k}"):
            return k
    return None


def _card(title: str, lines: list[str], level: str | None) -> str:
    border, bg = LEVEL.get(level, (C["line"], C["card"]))
    body, buf = "", []

    def flush():
        nonlocal body, buf
        if buf:
            body += f'<ul style="margin:6px 0 0;padding-left:18px">{"".join(buf)}</ul>'
            buf = []

    for x in lines:
        if not x.strip():
            continue
        if x.startswith("- "):  # 條列
            buf.append(f'<li style="margin:4px 0;line-height:1.55">{_inline(x[2:])}</li>')
        else:  # 一般說明文字
            flush()
            body += f'<div style="margin:6px 0 0;line-height:1.6">{_inline(x)}</div>'
    flush()
    head = (f'<div style="font-weight:700;font-size:15px;color:{C["head"]}">{_badge(level)}{_inline(_strip_level(title))}</div>'
            if title else "")
    return (f'<div style="background:{bg};border:1px solid {C["line"]};border-left:4px solid {border};'
            f'border-radius:8px;padding:12px 14px;margin:0 0 12px">{head}{body}</div>')


def render(subject: str, body: str) -> str:
    blocks = [b.strip("\n") for b in re.split(r"\n\s*\n", body.strip()) if b.strip()]
    if not blocks:
        return ""
    footer = ""  # 最後一段若是短的時間／來源說明，放到頁尾
    if len(blocks) > 1 and len(blocks[-1]) < 80 and "\n" not in blocks[-1] and ("通知" in blocks[-1] or "網站" in blocks[-1]):
        footer = blocks.pop()
    if blocks[0].startswith("■"):  # 即時通知：每則一張卡片，不當大標題
        blocks = [""] + blocks
    first = blocks[0].splitlines() or [""]
    title = first[0]
    chips = ""
    rest_first = first[1:]
    if rest_first and "｜" in rest_first[0]:
        parts = [p.strip() for p in rest_first[0].split("｜") if p.strip()]
        chips = "".join(
            f'<td style="padding:4px"><div style="background:{C["bg"]};border-radius:8px;padding:8px 10px;'
            f'font-size:13px;color:{C["ink"]};white-space:nowrap">{_inline(p)}</div></td>' for p in parts)
        chips = f'<table role="presentation" style="border-collapse:collapse;margin-top:8px"><tr>{chips}</tr></table>'
        rest_first = rest_first[1:]
    cards = []
    if rest_first:
        cards.append(_card("", rest_first, _level(title)))
    for b in blocks[1:]:
        ls = b.splitlines()
        head = ls[0]
        if head.startswith("■") or head.endswith("：") or head.endswith(":") or (len(ls) > 1 and ls[1].startswith("- ")):
            t = head.lstrip("■ ").rstrip("：:") if not head.startswith("■") else head.lstrip("■ ")
            cards.append(_card(t, ls[1:], _level(head)))
        else:
            cards.append(_card("", ls, _level(head)))
    lvl = _level(subject) or _level(title)
    bar = LEVEL[lvl][0] if lvl else C["head"]
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;background:{C['bg']};font-family:-apple-system,'Segoe UI','Noto Sans TC','Microsoft JhengHei',sans-serif;color:{C['ink']}">
<div style="max-width:640px;margin:0 auto;padding:16px 12px">
  <div style="background:{bar};color:#fff;border-radius:10px 10px 0 0;padding:14px 16px">
    <div style="font-size:12px;opacity:.85">投資競賽助手</div>
    <div style="font-size:18px;font-weight:700;margin-top:2px;line-height:1.4">{html.escape(_strip_level(subject.replace('[投資競賽] ', '')))}</div>
  </div>
  <div style="background:{C['card']};border-radius:0 0 10px 10px;padding:14px 16px 6px;margin-bottom:12px">
    <div style="font-size:15px;font-weight:600;line-height:1.5">{_inline(title) or html.escape(footer)}</div>{chips}
  </div>
  {''.join(cards)}
  <div style="text-align:center;margin:18px 0 8px">
    <a href="{SITE}" style="display:inline-block;background:{C['link']};color:#fff;text-decoration:none;border-radius:8px;padding:10px 22px;font-weight:600">開啟網站看即時建議</a>
  </div>
  <div style="text-align:center;font-size:11px;color:{C['muted']}">{html.escape(footer) + '・' if footer and title else ''}數字由程式計算；估計值不是保證。下單前請以即時報價為準。</div>
</div></body></html>"""
