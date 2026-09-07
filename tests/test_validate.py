"""Tests for credential-free config validation.

`--validate` exists because `--dry-run` cannot do this job: dry-run stubs the
database but still constructs sources for real, so it needs credentials and,
once given them, makes live requests. CI has neither and should have neither.
"""
from __future__ import annotations

import pytest
import yaml

from apix.collector import build_sources, load_basket, validate


@pytest.fixture
def basket():
    return load_basket("config/basket.yaml")


def test_validate_passes_without_any_credentials(monkeypatch, basket):
    for var in ("TRAVELPAYOUTS_TOKEN", "AGGREGATOR_API_TOKEN",
                "SUPABASE_URL", "SUPABASE_SERVICE_KEY"):
        monkeypatch.delenv(var, raising=False)
    validate(basket)          # must not raise


def test_offline_build_tolerates_a_missing_token(monkeypatch, basket):
    monkeypatch.delenv("TRAVELPAYOUTS_TOKEN", raising=False)
    build_sources(basket, offline=True)


def test_online_build_still_fails_loudly(monkeypatch, basket):
    """Offline tolerance must not leak into a real run — a live collection
    with no source is a failure, not an empty day."""
    monkeypatch.delenv("TRAVELPAYOUTS_TOKEN", raising=False)
    monkeypatch.delenv("AGGREGATOR_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="no sources enabled"):
        build_sources(basket, offline=False)


def test_validate_rejects_an_empty_source_list(basket):
    b = dict(basket)
    b["sources"] = [{**s, "enabled": False} for s in basket["sources"]]
    with pytest.raises(RuntimeError, match="no sources enabled"):
        validate(b)


def test_validate_catches_unnormalised_weights(basket):
    b = dict(basket)
    b["routes"] = [dict(r) for r in basket["routes"]]
    b["routes"][0]["weight"] = -1.0        # forces the sum negative
    with pytest.raises(Exception):
        validate(b)


def test_validate_catches_window_mismatch(basket):
    b = dict(basket)
    b["collection"] = {**basket["collection"], "windows": [1, 7, 15]}
    with pytest.raises(RuntimeError, match="disagrees"):
        validate(b)


def test_ci_runs_validate_not_dry_run():
    """Pin the workflow: --dry-run there would need a secret and would make
    live requests against a third-party API on every push."""
    ci = open(".github/workflows/ci.yml").read()
    assert "--validate" in ci
    assert "scripts.collect --dry-run" not in ci
