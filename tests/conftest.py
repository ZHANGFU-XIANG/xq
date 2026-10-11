"""Shared test fixtures: isolate the tuning agent's learned parameters so tests only use the settings.yaml defaults.

On 10/09 the tuning agent wrote base_offense 0.65 to config/learned_params.yaml, which made tests that hard-code
the defaults fail. Every test now runs with an empty learned file (tests that need learned values write it themselves)."""
import pytest

from src.state import params
from src.state import portfolio as pfm


@pytest.fixture(autouse=True)
def _isolated_learned_params(tmp_path, monkeypatch):
    monkeypatch.setattr(params, "LEARNED", tmp_path / "learned_params.yaml")
    # Daily history (including closing holdings) also comes from live data: isolate it, otherwise the vanished-holdings alert affects tests
    monkeypatch.setattr(pfm, "HISTORY_JSON", tmp_path / "nav_history.json")
