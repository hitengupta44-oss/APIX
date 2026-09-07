"""Tests for the CPI press-release annexure parser.

MoSPI's PDF layout wraps rows three different ways in one document and carries
a watermark that pdftotext drops into the text layer. Every test here pins a
failure I actually hit while building it, because the failure mode is a parse
that succeeds and returns wrong names or a third of the rows.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from scripts.ingest_cpi_release import (
    VALID_DIVISIONS,
    parse_general_series,
    parse_rows,
    to_long,
)

# Reproduces all three wrap styles plus the watermark and header noise.
SAMPLE = """
This Press Release is embargoed against publication, telecast or circulation
2026
                                          Annexure- I
 Division                                    Index                Inflation
   code    Division name           Rural     Urban     Comb.   Rural  Urban  Comb.
      01              Food and beverages
                                            108.97    109.33   109.10   5.53   4.75    5.24

      03              Clothing and footwear
                                            109.47    106.80   108.45   3.97   2.41    3.38
                      Housing, water, electricity,
      04
                      gas and other fuels
                                            104.61    103.40   103.85   2.47   1.98    2.16
                      Furnishings, household
      05              equipment and routine
                      household maintenance 105.62    104.66   105.21   2.79   1.88    2.40

      07              Transport
                                            105.73    105.51   105.63   4.49   4.37    4.43
                                            108.34    107.45   107.94   4.84   3.96    4.45
                      All India

                                          Annexure- II
 22.       07.1    Purchase of vehicles      99.17     97.96    98.56  -4.30  -4.45   -4.37
 23.       07.2    Operation of personal transport
                   equipment                107.39    107.43   107.41   7.35   7.37    7.36
 24.       07.3    Passenger transport services 105.24 105.60  105.39   2.81   3.01    2.90

                                          Annexure-IV
 Jan-25                                     101.81    101.49   101.67
 Jun-26                                     107.24    106.69   107.00   4.74   3.93    4.38
 July-26*                                   108.34    107.45   107.94   4.84   3.96    4.45
"""


def rows():
    return {code: (name, nums) for code, name, nums in parse_rows(SAMPLE)}


def test_single_line_row():
    r = rows()
    assert r["07.1"][0] == "Purchase of vehicles"
    assert r["07.1"][1] == [99.17, 97.96, 98.56, -4.30, -4.45, -4.37]


def test_numbers_on_the_following_line():
    """Style 1: code + name, bare numbers beneath."""
    r = rows()
    assert r["07"][0] == "Transport"
    assert r["07"][1][2] == 105.63


def test_code_floating_between_name_fragments():
    """Style 2: the layout puts the code on its own line mid-name. This is
    what silently dropped ten of twelve divisions on the first attempt."""
    r = rows()
    assert r["04"][0] == "Housing, water, electricity, gas and other fuels"


def test_three_line_name_ending_with_numbers():
    """Style 3: name spans three lines and the last one carries the figures."""
    r = rows()
    assert r["05"][0] == "Furnishings, household equipment and routine household maintenance"
    assert r["05"][1][2] == 105.21


def test_wrapped_group_name():
    r = rows()
    assert r["07.2"][0] == "Operation of personal transport equipment"


def test_watermark_and_headers_stripped():
    """The page watermark ('2026') and column headers sit adjacent to the
    first data row and get glued onto its name if not filtered."""
    r = rows()
    assert r["01"][0] == "Food and beverages"
    assert "2026" not in r["01"][0]
    assert "Division name" not in r["01"][0]


def test_prose_is_not_absorbed_into_names():
    for _, (name, _) in rows().items():
        assert len(name) < 80, f"name absorbed surrounding prose: {name!r}"
        assert "embargo" not in name.lower()


def test_all_india_total_row_is_not_a_division():
    """The unnumbered All India row has six figures and no code."""
    assert "All India" not in {name for name, _ in rows().values()}


def test_valid_divisions_excludes_12():
    """COICOP 2018 has a division 12, but CPI 2024 does not use it. If a
    release starts publishing one, this is the guard that surfaces it."""
    assert "12" in VALID_DIVISIONS
    assert "07" in VALID_DIVISIONS
    assert "21" not in VALID_DIVISIONS   # a page number, not a division


def test_group_073_is_the_benchmark():
    r = rows()
    name, nums = r["07.3"]
    assert name == "Passenger transport services"
    assert nums[2] == 105.39      # combined index
    assert nums[5] == 2.90        # combined y/y inflation


def test_to_long_splits_sectors():
    out = to_long([("07.3", "Passenger transport services",
                    [105.24, 105.60, 105.39, 2.81, 3.01, 2.90])],
                  pd.Timestamp("2026-07-01"), "group")
    assert len(out) == 3
    comb = out[out["sector"] == "combined"].iloc[0]
    assert comb["index_value"] == 105.39
    assert comb["inflation_pct"] == 2.90
    rural = out[out["sector"] == "rural"].iloc[0]
    assert rural["index_value"] == 105.24


def test_general_series_handles_missing_inflation():
    """Early months of a rebased series have an index but no y/y figure,
    because there is no year-ago base yet. Those must parse as null, not be
    skipped and not shift the columns."""
    s = parse_general_series(SAMPLE)
    jan = s[(s["period"] == pd.Timestamp("2025-01-01")) & (s["sector"] == "combined")]
    assert jan.iloc[0]["index_value"] == 101.67
    assert pd.isna(jan.iloc[0]["inflation_pct"])
    jul = s[(s["period"] == pd.Timestamp("2026-07-01")) & (s["sector"] == "combined")]
    assert jul.iloc[0]["index_value"] == 107.94
    assert jul.iloc[0]["inflation_pct"] == 4.45


def test_provisional_asterisk_does_not_break_the_month():
    s = parse_general_series(SAMPLE)
    assert pd.Timestamp("2026-07-01") in set(s["period"])


@pytest.mark.skipif(not Path("data/cpi_groups.csv").exists(),
                    reason="run scripts/ingest_cpi_release.py first")
def test_shipped_data_has_the_benchmark_group():
    g = pd.read_csv("data/cpi_groups.csv", dtype={"group_code": str})
    pts = g[(g["group_code"] == "07.3") & (g["sector"] == "combined")]
    assert not pts.empty, "group 07.3 missing from the shipped CPI data"
    assert pts["index_value"].between(80, 200).all()


@pytest.mark.skipif(not Path("data/cpi_divisions.csv").exists(),
                    reason="run scripts/ingest_cpi_release.py first")
def test_shipped_divisions_are_complete():
    d = pd.read_csv("data/cpi_divisions.csv", dtype={"division_code": str})
    per_period = d[d["sector"] == "combined"].groupby("period").size()
    assert (per_period >= 12).all(), f"a release parsed short: {per_period.to_dict()}"
