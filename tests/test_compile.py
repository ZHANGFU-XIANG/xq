import importlib
import py_compile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_all_sources_compile():
    """2026-10-14 src/main.py 被寫進未跳脫的換行而無法執行，但其他測試沒有載入 main.py 所以沒發現：每個檔案都要能編譯。"""
    bad = []
    for p in list((ROOT / "src").rglob("*.py")) + list((ROOT / "scripts").glob("*.py")) + [ROOT / "app.py"]:
        try:
            py_compile.compile(str(p), doraise=True)
        except py_compile.PyCompileError as e:
            bad.append(f"{p.relative_to(ROOT)}: {e.msg}")
    assert not bad, bad


def test_main_imports():
    importlib.import_module("src.main")  # GitHub Actions 的盤前／盤後／週報都從這裡啟動
