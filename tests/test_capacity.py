"""Tests for DGCA carrier statistics and the capacity module."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from apix.capacity import (
    CARGO_ONLY,
    carrier_shares,
    explain_move,
    hhi,
    industry_monthly,
    load,
)
from scripts.ingest_carrier_stats import CARRIER_TO_IATA, classify, derive

HAS_DATA = Path("data/dgca_carrier_stats.csv").exists()
needs_data = pytest.mark.skipif(
    not HAS_DATA, reason="run scripts/ingest_carrier_stats.py first")


def test_block_classification():
    """Scheduled and non-scheduled titles differ by one word and the
    international blocks contain the word 'Scheduled' too, so a naive
    substring match assigns every block to the first type."""
    assert classify("Monthly Traffic ... ( Scheduled Domestic Services)") \
        == "scheduled_domestic"
    assert classify("Monthly Traffic ... ( Scheduled International Services)") \
        == "scheduled_international"
    assert classify("Monthly Traffic ... (Non- Scheduled Domestic Services)") \
        == "nonscheduled_domestic"
    assert classify("Monthly Traffic ... (Non-Scheduled International)") \
        == "nonscheduled_international"
    assert classify("something else entirely") == "unknown"


def test_carrier_codes_cover_current_operators():
    for name in ("INDIGO", "AIR INDIA", "AIR INDIA EXPRESS", "SPICEJET",
                 "AKASA AIR", "ALLIANCE AIR"):
        assert name in CARRIER_TO_IATA
    assert CARRIER_TO_IATA["INDIGO"] == "6E"
    assert CARRIER_TO_IATA["AKASA AIR"] == "QP"


def test_derive_handles_cargo_carriers_without_inf():
    """Cargo-only carriers report zero ASK. Dividing by it produces inf, which
    propagates silently through every downstream mean."""
    df = pd.DataFrame({
        "carrier": ["BZ", "6E"],
        "carrier_name": ["BLUEDART", "INDIGO"],
        "service_type": ["scheduled_domestic"] * 2,
        "period": pd.to_datetime(["2026-07-01"] * 2),
        "rpk_thousand": [0.0, 7600417.0],
        "ask_thousand": [0.0, 9218918.0],
        "pax_load_factor": [np.nan, 82.44],
        "passengers": [0, 8081825],
    })
    out = derive(df)
    assert not np.isinf(out["load_factor_derived"]).any()
    assert pd.isna(out.loc[out["carrier"] == "BZ", "load_factor_derived"].iloc[0])


def test_derived_load_factor_matches_published():
    """RPK/ASK must reproduce the published load factor. If it does not, a
    column has shifted in the template and every capacity figure is wrong."""
    df = pd.DataFrame({
        "carrier": ["6E"], "carrier_name": ["INDIGO"],
        "service_type": ["scheduled_domestic"],
        "period": pd.to_datetime(["2026-07-01"]),
        "rpk_thousand": [7600417.0], "ask_thousand": [9218918.0],
        "pax_load_factor": [82.44], "passengers": [8081825],
    })
    out = derive(df)
    assert out["load_factor_gap"].iloc[0] < 0.05


@needs_data
def test_shipped_data_passes_the_load_factor_check():
    df = load()
    gap = df["load_factor_gap"].dropna()
    assert (gap < 0.5).all(), (
        "published and derived load factors disagree — a column shifted")


@needs_data
def test_cargo_carriers_excluded_from_capacity():
    df = load()
    assert not set(df["carrier"]) & CARGO_ONLY, (
        "cargo-only carriers would drag industry load factor toward zero")


@needs_data
def test_industry_load_factor_is_capacity_weighted():
    """Averaging published carrier load factors would weight a regional
    operator equal to IndiGo. It must come from summed RPK over summed ASK."""
    df = load()
    agg = industry_monthly(df)
    period = agg["period"].max()
    row = agg[agg["period"] == period].iloc[0]

    month = df[df["period"] == period]
    naive = month["pax_load_factor"].mean()
    weighted = 100 * month["rpk_thousand"].sum() / month["ask_thousand"].sum()

    assert abs(row["load_factor"] - weighted) < 0.05
    assert abs(row["load_factor"] - naive) > 1.0, (
        "weighted and naive averages coincide — the test is not discriminating")


@needs_data
def test_shares_sum_to_one_hundred():
    s = carrier_shares()
    assert abs(s["ask_share_pct"].sum() - 100) < 0.5
    assert abs(s["pax_share_pct"].sum() - 100) < 0.5
    assert s["ask_share_pct"].is_monotonic_decreasing


@needs_data
def test_hhi_flags_concentration():
    h = hhi()
    assert h["hhi"] > 2500
    assert h["interpretation"] == "highly concentrated"
    assert h["largest_carrier"] == "6E"
    assert 50 < h["largest_share_pct"] < 80


def test_explain_move_labels():
    cap = pd.DataFrame({
        "period": pd.to_datetime(["2026-05-01", "2026-06-01", "2026-07-01"]),
        "ask_thousand": [1.77e7, 1.54e7, 1.39e7],
        "ask_mom_pct": [6.36, -13.0, -9.49],
        "load_factor": [85.95, 85.72, 83.16],
        "load_factor_change_pp": [3.96, -0.23, -2.56],
    })
    apix = pd.DataFrame({
        "period": pd.to_datetime(["2026-05-01", "2026-06-01", "2026-07-01"]),
        "apix": [100.0, 104.0, 108.0],
        "change_pct": [np.nan, 4.0, 3.8],
    })
    out = explain_move(apix, cap)
    jun = out[out["period"] == pd.Timestamp("2026-06-01")].iloc[0]
    # Fares up, capacity down, load factor also down.
    assert jun["reading"] == "capacity withdrawn, demand also softer"

    cap2 = cap.copy()
    cap2["load_factor_change_pp"] = [3.96, 2.0, 1.5]
    out2 = explain_move(apix, cap2)
    jun2 = out2[out2["period"] == pd.Timestamp("2026-06-01")].iloc[0]
    assert jun2["reading"] == "capacity withdrawn into firm demand"


def test_explain_move_returns_empty_without_overlap():
    cap = pd.DataFrame({
        "period": pd.to_datetime(["2026-01-01"]),
        "ask_thousand": [1e7], "ask_mom_pct": [1.0],
        "load_factor": [85.0], "load_factor_change_pp": [0.5],
    })
    apix = pd.DataFrame({
        "period": pd.to_datetime(["2027-06-01"]),
        "apix": [100.0], "change_pct": [1.0],
    })
    assert explain_move(apix, cap).empty


def test_capacity_module_degrades_without_the_file():
    assert load("data/definitely_missing.csv").empty
    assert industry_monthly(pd.DataFrame()).empty
