"""Tests for sandbox-fare rejection in the aggregator adapter.

A synthetic fare is worse than a missing one: it flows through cleaning,
aggregation and publication without complaint and produces an index that looks
correct. These tests exist so that cannot happen quietly.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from apix.compliance import SourcePolicy
from apix.models import FareQuote
from apix.sources.aggregator_api import SANDBOX_CARRIERS, AggregatorApiSource

POLICY = SourcePolicy(
    name="gds:aggregator", basis="licensed_api",
    permission_ref="TEST-CONTRACT", min_delay_seconds=1.0,
)


def make_source(monkeypatch, token="duffel_live_abc123", allow=None):
    monkeypatch.setenv("AGGREGATOR_API_TOKEN", token)
    if allow is None:
        monkeypatch.delenv("APIX_ALLOW_SANDBOX", raising=False)
    else:
        monkeypatch.setenv("APIX_ALLOW_SANDBOX", allow)
    return AggregatorApiSource(POLICY)


def quote(carrier: str, fare: float = 5000.0) -> FareQuote:
    return FareQuote(
        quote_ts=datetime(2026, 9, 8, 6, tzinfo=timezone.utc),
        origin="DEL", destination="BOM",
        departure_date=date(2026, 9, 15), apw_days=7,
        carrier=carrier, flight_number=f"{carrier}101",
        total_fare=fare, source="gds:aggregator",
    )


def test_missing_token_refuses_to_construct(monkeypatch):
    monkeypatch.delenv("AGGREGATOR_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="AGGREGATOR_API_TOKEN"):
        AggregatorApiSource(POLICY)


def test_test_token_warns(monkeypatch, caplog):
    with caplog.at_level("WARNING"):
        make_source(monkeypatch, token="duffel_test_abc123")
    assert any("TEST token" in r.message for r in caplog.records)


def test_live_token_does_not_warn(monkeypatch, caplog):
    with caplog.at_level("WARNING"):
        make_source(monkeypatch, token="duffel_live_abc123")
    assert not any("TEST token" in r.message for r in caplog.records)


def test_sandbox_quotes_dropped_by_default(monkeypatch):
    s = make_source(monkeypatch)
    mixed = [quote("6E"), quote("ZZ"), quote("AI"), quote("ZZ")]
    kept = s._reject_sandbox(mixed, "DEL", "BOM")
    assert len(kept) == 2
    assert {q.carrier for q in kept} == {"6E", "AI"}


def test_all_sandbox_yields_nothing(monkeypatch):
    s = make_source(monkeypatch)
    kept = s._reject_sandbox([quote("ZZ") for _ in range(8)], "DEL", "BOM")
    assert kept == []


def test_opt_in_keeps_sandbox_but_tags_the_source(monkeypatch):
    """When you deliberately test the plumbing, synthetic rows survive — but
    tagged, so they can be excluded later and are never mistaken for real."""
    s = make_source(monkeypatch, allow="1")
    kept = s._reject_sandbox([quote("6E"), quote("ZZ")], "DEL", "BOM")
    assert len(kept) == 2
    tagged = [q for q in kept if q.source.endswith(":SANDBOX")]
    assert len(tagged) == 1 and tagged[0].carrier == "ZZ"
    real = [q for q in kept if not q.source.endswith(":SANDBOX")]
    assert real[0].carrier == "6E"


def test_repeated_sandbox_only_runs_escalate(monkeypatch, caplog):
    """Five consecutive empty results should say why, not just log five
    identical warnings nobody reads."""
    s = make_source(monkeypatch)
    with caplog.at_level("ERROR"):
        for _ in range(5):
            s._reject_sandbox([quote("ZZ")], "DEL", "BOM")
    assert any("test token" in r.message for r in caplog.records)


def test_real_quotes_reset_the_counter(monkeypatch):
    s = make_source(monkeypatch)
    for _ in range(3):
        s._reject_sandbox([quote("ZZ")], "DEL", "BOM")
    assert s._sandbox_only_runs == 3
    s._reject_sandbox([quote("6E")], "DEL", "BOM")
    assert s._sandbox_only_runs == 0


def test_empty_input_is_not_an_error(monkeypatch):
    s = make_source(monkeypatch)
    assert s._reject_sandbox([], "DEL", "BOM") == []


def test_sandbox_set_covers_duffel_airways():
    assert "ZZ" in SANDBOX_CARRIERS
