"""Tests for CPI-impact translation.

The arithmetic is trivial; the failure modes are not. Every test here is about
what happens when the weight file is missing, unfilled, or malformed — because
those are the states the repo will actually be in until someone visits
cpi.mospi.gov.in, and none of them may produce a plausible-looking wrong
number.
"""
from __future__ import annotations

import pandas as pd
import pytest

from apix.cpi_impact import (
    CPI_2024_WEIGHTS,
    LINKING_FACTORS,
    ItemWeight,
    cpi_contribution,
    describe,
    load_item_weight,
    monthly_impact,
    transport_weight_change,
)

W = ItemWeight(item="Air fare", rural=0.093, urban=0.415, combined=0.198,
               source="test")


def test_contribution_arithmetic():
    c = cpi_contribution(6.2, W)
    # 0.198% of the basket moving 6.2% -> 0.198/100 * 6.2 pp.
    # The module rounds pp to 5dp for output, so tolerance sits just above that.
    assert c["cpi_contribution_pp"] == pytest.approx(0.012276, abs=1e-5)
    assert c["cpi_contribution_bps"] == pytest.approx(1.23, abs=0.01)


def test_contribution_is_linear_and_signed():
    assert cpi_contribution(10.0, W)["cpi_contribution_pp"] == pytest.approx(
        2 * cpi_contribution(5.0, W)["cpi_contribution_pp"])
    assert cpi_contribution(-4.0, W)["cpi_contribution_bps"] < 0
    assert cpi_contribution(0.0, W)["cpi_contribution_pp"] == 0.0


def test_urban_impact_exceeds_rural():
    """Air travel is an urban item; the sector split has to survive."""
    u = cpi_contribution(5.0, W, sector="urban")["cpi_contribution_bps"]
    r = cpi_contribution(5.0, W, sector="rural")["cpi_contribution_bps"]
    assert u > r


def test_missing_file_returns_none_not_a_guess():
    assert load_item_weight("data/definitely_not_here.csv") is None


def test_unfilled_template_returns_none(tmp_path):
    """The shipped template has headers and no rows. It must not silently
    produce a zero-weight ItemWeight, which would report 0 bps impact and look
    like a real finding."""
    p = tmp_path / "w.csv"
    p.write_text("# fill me in\nitem,rural,urban,combined\n")
    assert load_item_weight(str(p)) is None


def test_malformed_file_returns_none(tmp_path):
    p = tmp_path / "w.csv"
    p.write_text("thing,value\nAir fare,0.2\n")
    assert load_item_weight(str(p)) is None


def test_no_matching_item_returns_none(tmp_path):
    p = tmp_path / "w.csv"
    p.write_text("item,rural,urban,combined\nRail fare,0.4,0.5,0.44\n")
    assert load_item_weight(str(p)) is None


def test_loads_a_filled_file(tmp_path):
    p = tmp_path / "w.csv"
    p.write_text("# comment line\nitem,rural,urban,combined\n"
                 "Rail fare,0.400,0.500,0.440\n"
                 "Air fare,0.093,0.415,0.198\n")
    w = load_item_weight(str(p))
    assert w is not None and w.combined == 0.198
    assert w.share("combined") == pytest.approx(0.00198)


def test_monthly_impact_needs_the_monthly_frame():
    m = pd.DataFrame({"quote_date": pd.to_datetime(["2026-04-01", "2026-05-01"]),
                      "apix": [100.0, 106.2], "change_pct": [None, 6.2]})
    out = monthly_impact(m, W)
    assert out["cpi_contribution_bps"].iloc[1] == pytest.approx(1.23, abs=0.01)
    with pytest.raises(ValueError, match="change_pct"):
        monthly_impact(pd.DataFrame({"apix": [1.0]}), W)


def test_division_weights_match_the_press_release():
    t = CPI_2024_WEIGHTS["07_transport"]
    assert (t["rural"], t["urban"], t["combined"]) == (8.644, 8.985, 8.796)
    assert CPI_2024_WEIGHTS["08_information_communication"]["combined"] == 3.609
    assert LINKING_FACTORS["combined"] == 0.5267


def test_transport_comparison_carries_its_caveat():
    d = transport_weight_change()
    assert d["pct_increase"] == pytest.approx(37.6, abs=0.1)
    # The 2012 figure bundled communication; the comparison is not like-for-like
    # and must say so wherever it is quoted.
    assert "COICOP" in d["caveat"]


def test_description_flags_that_it_is_an_estimate():
    s = describe(cpi_contribution(6.2, W))
    assert "basis points" in s
    assert "Estimated" in s and "not a measurement" in s
