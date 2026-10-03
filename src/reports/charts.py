"""報告圖表（matplotlib 靜態 PNG，嵌入 docx / pptx）。
配色採台股慣例：紅漲綠跌；每根長條都標數值與正負號，不只靠顏色辨識。
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

UP, DOWN, NEUTRAL = "#C8423B", "#2E8B57", "#8A8F98"
INK, MUTED, GRID = "#1F2328", "#57606A", "#E5E7EB"
ACCENT = "#1F5FAD"

_CJK = ["Microsoft JhengHei", "Noto Sans CJK TC", "Noto Sans TC", "PingFang TC", "Heiti TC", "SimHei"]
_available = {f.name for f in font_manager.fontManager.ttflist}
plt.rcParams["font.family"] = [f for f in _CJK if f in _available][:1] + ["DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def _style(ax, title: str) -> None:
    ax.set_title(title, loc="left", fontsize=13, color=INK, pad=12, fontweight="bold")
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=MUTED, length=0)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _hbar(labels: list[str], values: list[float], title: str, path: Path, unit: str = "%",
          annotations: list[str] | None = None) -> Path:
    fig, ax = plt.subplots(figsize=(8, max(2.5, 0.38 * len(labels) + 1)), dpi=160)
    colors = [UP if v > 0 else DOWN if v < 0 else NEUTRAL for v in values]
    y = range(len(labels))
    ax.barh(list(y), values, color=colors, height=0.6, edgecolor="white", linewidth=1)
    ax.set_yticks(list(y), labels, fontsize=10, color=INK)
    ax.invert_yaxis()
    ax.axvline(0, color=MUTED, linewidth=1)
    span = max(abs(v) for v in values) if values else 1
    ax.set_xlim(-span * 1.45 - 0.1, span * 1.45 + 0.1)
    for i, v in enumerate(values):
        txt = f"{v:+.2f}{unit}" + (f"  {annotations[i]}" if annotations and annotations[i] else "")
        ax.text(v + (span * 0.03 if v >= 0 else -span * 0.03), i, txt, va="center",
                ha="left" if v >= 0 else "right", fontsize=9, color=INK)
    _style(ax, title)
    fig.tight_layout()
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return path


def us_moves_chart(us_snapshot: list[dict], path: Path) -> Path:
    rows = sorted([r for r in us_snapshot if r["ticker"] not in ("TWD=X", "^TNX", "^VIX")],
                  key=lambda r: -r["change_pct"])
    return _hbar([r["name"] for r in rows], [r["change_pct"] for r in rows], "美股前一交易日漲跌幅", path)


def prediction_chart(preds: dict, names: dict, path: Path) -> Path:
    items = sorted(preds.items(), key=lambda kv: -kv[1]["gap_pct"])
    labels = [("加權指數" if c == "TAIEX" else f"{c} {names.get(c, '')}") for c, _ in items]
    ann = [f"命中率 {p['gap_hit_rate'] * 100:.0f}%" + ("" if p["reliable"] else "（降權）") for _, p in items]
    return _hbar(labels, [p["gap_pct"] for _, p in items], "模型預測今日開盤跳空幅度", path, annotations=ann)


def allocation_chart(weights: list[tuple[str, float]], path: Path) -> Path:
    weights = sorted(weights, key=lambda kv: -kv[1])
    fig, ax = plt.subplots(figsize=(8, max(2.5, 0.38 * len(weights) + 1)), dpi=160)
    y = range(len(weights))
    ax.barh(list(y), [w for _, w in weights], color=ACCENT, height=0.6)
    ax.set_yticks(list(y), [n for n, _ in weights], fontsize=10, color=INK)
    ax.invert_yaxis()
    for i, (_, w) in enumerate(weights):
        ax.text(w + 0.4, i, f"{w:.1f}%", va="center", fontsize=9, color=INK)
    ax.set_xlim(0, max([w for _, w in weights] + [10]) * 1.2)
    _style(ax, "執行建議後持股權重（佔總資產）")
    fig.tight_layout()
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return path


def nav_chart(history: list[dict], path: Path) -> Path | None:
    if len(history) < 2:
        return None
    fig, ax = plt.subplots(figsize=(8, 3.2), dpi=160)
    xs = [h["date"][5:] for h in history]
    ys = [h["return_pct"] for h in history]
    ax.plot(xs, ys, color=ACCENT, linewidth=2, marker="o", markersize=4)
    ax.axhline(0, color=MUTED, linewidth=1)
    ax.annotate(f"{ys[-1]:+.2f}%", (len(xs) - 1, ys[-1]), textcoords="offset points", xytext=(6, 4),
                fontsize=10, color=INK, fontweight="bold")
    ax.yaxis.grid(True, color=GRID)
    ax.xaxis.grid(False)
    _style(ax, "累積報酬率（%）")
    ax.tick_params(axis="x", labelrotation=45, labelsize=8)
    fig.tight_layout()
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return path


SERIES = ["#1F5FAD", "#E08A1E", "#7A5AA6"]  # 大盤 / 產業 / 個股特有（類別色，固定順序）


def attribution_chart(att: dict, path: Path) -> Path:
    rows = att["rows"]
    labels = [f"{r['code']} {r['name']}" for r in rows]
    comps = [("大盤因素", "mkt_pct"), ("產業因素", "ind_pct"), ("個股特有", "idio_pct")]
    fig, ax = plt.subplots(figsize=(8, max(2.8, 0.75 * len(rows) + 1.2)), dpi=160)
    h = 0.24
    for k, (name, key) in enumerate(comps):
        ys = [i + (k - 1) * h for i in range(len(rows))]
        vals = [r[key] for r in rows]
        ax.barh(ys, vals, height=h, color=SERIES[k], label=name, edgecolor="white", linewidth=1)
        for y, v in zip(ys, vals):
            ax.text(v + (0.05 if v >= 0 else -0.05), y, f"{v:+.2f}%", va="center", ha="left" if v >= 0 else "right",
                    fontsize=7.5, color=INK)
    ax.set_yticks(range(len(rows)), [f"{lbl}\n合計 {r['ret_pct']:+.2f}%" for lbl, r in zip(labels, rows)],
                  fontsize=9, color=INK)
    ax.invert_yaxis()
    ax.axvline(0, color=MUTED, linewidth=1)
    span = max([abs(r[k]) for r in rows for _, k in comps] + [0.5])
    ax.set_xlim(-span * 1.6, span * 1.6)
    ax.legend(loc="lower right", fontsize=8, frameon=False, ncol=3)
    _style(ax, f"{att['date']} 漲跌歸因（%）")
    fig.tight_layout()
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return path


def top_picks_chart(picks: list[dict], base_rate: float, path: Path) -> Path:
    picks = picks[:15]
    fig, ax = plt.subplots(figsize=(8, max(3, 0.36 * len(picks) + 1.2)), dpi=160)
    y = range(len(picks))
    vals = [p["p_newhigh"] * 100 for p in picks]
    ax.barh(list(y), vals, color=ACCENT, height=0.6)
    ax.set_yticks(list(y), [f"{p['code']} {p['name']}" for p in picks], fontsize=9, color=INK)
    ax.invert_yaxis()
    ax.axvline(base_rate * 100, color=UP, linewidth=1.2, linestyle="--")
    ax.text(base_rate * 100, -0.9, f"全市場基準 {base_rate * 100:.0f}%", color=UP, fontsize=8, ha="center")
    for i, p in enumerate(picks):
        ax.text(vals[i] + 1, i, f"{vals[i]:.0f}%｜預期超額 {p['exp_excess'] * 100:+.1f}%", va="center", fontsize=8, color=INK)
    ax.set_xlim(0, 135)
    _style(ax, "一個月內創 52 週新高機率（模型前段班）")
    fig.tight_layout()
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return path


def sector_chart(att: dict, path: Path) -> Path:
    items = list(att["sector_median_pct"].items())
    sel = items[:6] + items[-6:] if len(items) > 12 else items
    return _hbar([k for k, _ in sel], [v for _, v in sel], f"{att['date']} 產業漲跌（類股中位數，最強與最弱）", path)
