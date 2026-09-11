"""Tests für brief.py — Config-Loading + Prompt-Bau (kein echter LLM-Call)."""
from __future__ import annotations

from scripts import brief


def test_default_config_fallbacks(tmp_path):
    cfg = brief.load_llm_config(tmp_path / "nope.yaml")
    assert cfg["model"] == brief.DEFAULT_MODEL
    assert cfg["base_url"] == brief.DEFAULT_BASE_URL


def test_load_config_from_yaml(tmp_path):
    p = tmp_path / "pipeline.yaml"
    p.write_text("llm:\n  model: custom/model\n  base_url: https://x/v1\n", encoding="utf-8")
    cfg = brief.load_llm_config(p)
    assert cfg["model"] == "custom/model"
    assert cfg["base_url"] == "https://x/v1"


def test_build_prompt_contains_actions_and_facts():
    data = {
        "holdings": [{"isin": "IE00BK5BQT80", "name": "Vanguard", "category": "core",
                      "value_eur": 5000.0, "quantity": 35.0}],
        "watchlist": [{"isin": "US0378331005", "name": "Apple", "security_type": "STOCK"}],
        "fundamentals": [{"ticker": "AAPL", "forward_pe": 30.0}],
        "news": [{"source": "mcp", "title": "Apple news"}],
        "signals": [{"severity": "red", "message": "X über 5%"}],
        "core_ratio": 70.0,
        "satellite_ratio": 30.0,
        "total_value_eur": 10000.0,
        "cash_eur": 350.0,
    }
    prompt = brief.build_prompt(data)
    for action in brief.ACTIONS:
        assert action in prompt
    assert "Vanguard" in prompt
    assert "AAPL" in prompt


def test_fact_package_none_safe_optional_numbers():
    # Optionale Zahlenwerte None → 'n/a', kein Crash (Regression 404-Fix-Blocker).
    data = {
        "holdings": [{"isin": "IE00BK5BQT80", "name": "Vanguard", "category": "core",
                      "value_eur": None, "quantity": None}],
        "watchlist": [],
        "fundamentals": [{"ticker": "AAPL", "forward_pe": None, "revenue_growth": None,
                          "earnings_growth": None, "debt_to_equity": None,
                          "position_52w": None}],
        "news": [],
        "signals": [],
        "core_ratio": None, "satellite_ratio": None,
        "total_value_eur": None, "cash_eur": None,
    }
    prompt = brief.build_prompt(data)
    assert "n/a" in prompt
    assert "Vanguard" in prompt


def test_fact_package_keeps_filled_values():
    # Gefüllte Felder unverändert formatiert (kein Verhaltensbruch).
    data = {
        "holdings": [{"isin": "IE00BK5BQT80", "name": "Vanguard", "category": "core",
                      "value_eur": 5000.0, "quantity": 35.0}],
        "watchlist": [], "fundamentals": [], "news": [], "signals": [],
        "core_ratio": 70.0, "satellite_ratio": 30.0,
        "total_value_eur": 10000.0, "cash_eur": 350.0,
    }
    prompt = brief.build_prompt(data)
    assert "5000 EUR" in prompt
    assert "35.00" in prompt
    assert "10000 EUR" in prompt
    assert "350 EUR" in prompt
