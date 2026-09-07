"""Tests for the Travelpayouts cached-fare adapter.

The cache-age handling is the part that matters. A stale quote silently filed
under the wrong advance-purchase window corrupts the lead-time curve, which is
the one output this archive genuinely supports.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from apix.compliance import SourcePolicy
from apix.sources.travelpayouts import (
    INDIAN_DOMESTIC_CARRIERS,
    TravelpayoutsSource,
)

POLICY = SourcePolicy(
    name="ota:travelpayouts", basis="licensed_api",
    permission_ref="TP-AFFILIATE-ACCOUNT", min_delay_seconds=1.0,
)
NOW = datetime(2026, 9, 8, 6, 0, tzinfo=timezone.utc)
DEP = date(2026, 9, 15)          # nominal T+7


def src(monkeypatch, **kw):
    monkeypatch.setenv("TRAVELPAYOUTS_TOKEN", "tp_test_token")
    return TravelpayoutsSource(POLICY, **kw)


def row(**over):
    base = {
        "price": 5432,
        "airline": "6E",
        "flight_number": 2034,
        "departure_at": "2026-09-15T06:30:00+05:30",
        "transfers": 0,
        "duration_to": 125,
        # 7 days retention, so an expiry 7 days out means cached just now.
        "expires_at": (NOW + timedelta(days=7)).isoformat(),
    }
    base.update(over)
    return base


def test_missing_token_refuses(monkeypatch):
    monkeypatch.delenv("TRAVELPAYOUTS_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="TRAVELPAYOUTS_TOKEN"):
        TravelpayoutsSource(POLICY)


def test_parses_a_fresh_quote(monkeypatch):
    s = src(monkeypatch)
    q = s._parse(row(), "DEL", "BOM", DEP, NOW)
    assert q is not None
    assert q.carrier == "6E"
    assert q.flight_number == "6E2034"
    assert q.total_fare == 5432.0
    assert q.currency == "INR"
    assert q.departure_time_local == "06:30"
    assert q.collection_method == "cached_api"


def test_fresh_quote_keeps_the_nominal_window(monkeypatch):
    s = src(monkeypatch)
    q = s._parse(row(), "DEL", "BOM", DEP, NOW)
    assert q.apw_days == 7


def test_cached_quote_shifts_to_a_later_window(monkeypatch):
    """A price cached two days ago was seen by someone booking two days
    further out. Filing it as T+7 would compress the lead-time curve."""
    s = src(monkeypatch, max_cache_age_days=3)
    two_days_old = row(expires_at=(NOW + timedelta(days=5)).isoformat())
    q = s._parse(two_days_old, "DEL", "BOM", DEP, NOW)
    assert q is not None
    assert q.apw_days == 9          # nominal 7 + 2 days of cache age


def test_stale_quotes_dropped(monkeypatch):
    s = src(monkeypatch, max_cache_age_days=2)
    five_days_old = row(expires_at=(NOW + timedelta(days=2)).isoformat())
    assert s._parse(five_days_old, "DEL", "BOM", DEP, NOW) is None
    assert s.stale_share() == 1.0


def test_missing_expiry_is_not_assumed_fresh_or_dropped(monkeypatch):
    """No expiry means unknown age. Keep it at the nominal window rather than
    inventing a correction, but it must not be silently discarded either."""
    s = src(monkeypatch)
    q = s._parse(row(expires_at=None), "DEL", "BOM", DEP, NOW)
    assert q is not None and q.apw_days == 7


def test_malformed_expiry_does_not_crash(monkeypatch):
    s = src(monkeypatch)
    q = s._parse(row(expires_at="not-a-date"), "DEL", "BOM", DEP, NOW)
    assert q is not None


def test_non_indian_carriers_filtered(monkeypatch):
    """Aviasales aggregates globally and returns operators no consumer can
    book on a domestic Indian sector."""
    s = src(monkeypatch)
    assert s._parse(row(airline="EK"), "DEL", "BOM", DEP, NOW) is None
    assert s._parse(row(airline="6E"), "DEL", "BOM", DEP, NOW) is not None


def test_filter_can_be_disabled(monkeypatch):
    s = src(monkeypatch, restrict_to_indian_carriers=False)
    assert s._parse(row(airline="EK"), "DEL", "BOM", DEP, NOW) is not None


def test_rows_without_price_or_carrier_skipped(monkeypatch):
    s = src(monkeypatch)
    assert s._parse(row(price=None), "DEL", "BOM", DEP, NOW) is None
    assert s._parse(row(airline=""), "DEL", "BOM", DEP, NOW) is None


def test_stale_share_reported(monkeypatch):
    s = src(monkeypatch, max_cache_age_days=1)
    fresh = row()
    stale = row(expires_at=(NOW + timedelta(days=3)).isoformat())
    for r in (fresh, fresh, stale, stale):
        s._parse(r, "DEL", "BOM", DEP, NOW)
    assert s.stale_share() == 0.5


def test_carrier_set_covers_the_market():
    for c in ("6E", "AI", "IX", "QP", "SG", "9I"):
        assert c in INDIAN_DOMESTIC_CARRIERS
    assert "EK" not in INDIAN_DOMESTIC_CARRIERS


def test_cached_method_distinguishes_from_live(monkeypatch):
    """Cached and live quotes must never pool by accident — the index would
    average prices of different vintages."""
    s = src(monkeypatch)
    q = s._parse(row(), "DEL", "BOM", DEP, NOW)
    assert q.collection_method == "cached_api"
    assert q.source == "ota:travelpayouts"
