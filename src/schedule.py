"""排程時刻表與防重複。

GitHub Actions 的 cron 在整點附近常延遲數小時甚至漏跑（2026-10-05：07:00 盤前 09:29 才跑、08:20 開盤前 13:52 才跑、
盤中 7 輪只跑 3 輪且都在收盤後），因此改由網站（Hugging Face Space，常駐）在準點以 workflow_dispatch 觸發
（幾秒內開始），cron 只當備援。

兩種來源都先經過 gate：已經過了有效時間就略過（例如收盤後才跑的盤中建議沒有意義）、同一時段已完成也略過。
完成紀錄寫在 data/schedule_log.json，隨 repo 同步到網站。

  python -m src.schedule gate <mode>   # 寫入 GITHUB_OUTPUT：run=true/false、key=…
  python -m src.schedule mark <key>    # 該時段完成
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

from src.config import DATA_DIR, is_trading_day, now_tw

LOG = DATA_DIR / "schedule_log.json"
INTRADAY_SLOTS = [(9, 0), (10, 20), (11, 40), (13, 0)]  # 與 agents/intraday.SLOTS 相同（這裡不匯入，避免載入大型套件）

# (key, mode, 開始時刻, 有效分鐘, 是否只在交易日)；有效時間內才執行，過了就等下一次
SCHEDULE: list[tuple[str, str, tuple[int, int], int, bool]] = [
    ("premarket", "premarket", (7, 0), 300, True),      # 最晚 12:00（開盤後仍有參考價值）
    ("preopen", "preopen", (8, 20), 55, True),          # 最晚 09:15，開盤後就沒意義
    *[(f"intraday {h:02d}:{m:02d}", "intraday", (h, m), 35, True) for h, m in INTRADAY_SLOTS],
    ("postclose", "postclose", (15, 45), 480, True),
    ("weekly", "weekly", (16, 30), 420, False),         # 只在週五
]


def _entries(day: dt.date):
    for key, mode, (h, m), window, trading_only in SCHEDULE:
        if mode == "weekly" and day.weekday() != 4:
            continue
        if trading_only and not is_trading_day(day):
            continue
        start = dt.datetime.combine(day, dt.time(h, m), now_tw().tzinfo)
        yield key, mode, start, start + dt.timedelta(minutes=window)


def due(now: dt.datetime | None = None) -> list[tuple[str, str, dt.datetime]]:
    """目前在有效時間內的排程（key, mode, 開始時刻）。"""
    now = now or now_tw()
    return [(k, mo, s) for k, mo, s, e in _entries(now.date()) if s <= now < e]


def load_log() -> dict:
    try:
        return json.loads(LOG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def done(key: str, day: dt.date) -> bool:
    return key in load_log().get(day.isoformat(), {})


def mark(key: str, now: dt.datetime | None = None) -> None:
    now = now or now_tw()
    log = load_log()
    keep = (now.date() - dt.timedelta(days=14)).isoformat()
    log = {d: v for d, v in log.items() if d >= keep}
    log.setdefault(now.date().isoformat(), {})[key] = now.strftime("%H:%M")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(json.dumps(log, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def gate(mode: str, now: dt.datetime | None = None) -> tuple[bool, str, str]:
    """排程觸發的執行是否該跑：(run, key, 原因)。"""
    now = now or now_tw()
    hit = [(k, s) for k, mo, s in due(now) if mo == mode]
    if not hit:
        return False, "", f"{now:%m/%d %H:%M} 不在 {mode} 的有效時間內（排程延遲過久或非交易日），略過"
    key, _ = hit[-1]
    if done(key, now.date()):
        return False, key, f"{key} 今天已完成，略過重複執行"
    return True, key, f"執行 {key}"


def main(argv: list[str]) -> None:
    cmd, arg = argv[0], argv[1]
    if cmd == "gate":
        run, key, why = gate(arg)
        print(why)
        out = os.getenv("GITHUB_OUTPUT")
        if out:
            with open(out, "a", encoding="utf-8") as f:
                f.write(f"run={'true' if run else 'false'}\nkey={key}\n")
    elif cmd == "mark":
        mark(arg)
        print(f"已記錄 {arg} 完成")


if __name__ == "__main__":
    main(sys.argv[1:])
