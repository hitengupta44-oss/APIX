"""Tests for weight construction from DGCA traffic.

The city-name normaliser is the fragile part. DGCA renames entries between
releases, and a rename that silently drops a route does not raise anything —
it just quietly removes ~7% of the basket and the index keeps producing
plausible-looking numbers. These tests are the alarm.
"""
from __future__ import annotations

import pandas as pd
import pytest
import yaml

from scripts.build_weights import CITY_MERGE, build, normalise_city, write_yaml


def test_plain_city_names():
    assert normalise_city("DELHI") == "DEL"
    assert normalise_city("delhi") == "DEL"
    assert normalise_city("  Mumbai  ") == "BOM"
    assert normalise_city("BANGALORE") == "BLR"      # legacy spelling
    assert normalise_city("BENGALURU") == "BLR"


def test_parenthetical_forms_resolve():
    """The DGCA naming drift that cost 12 points of coverage."""
    assert normalise_city("MUMBAI (MUMBAI)") == "BOM"
    assert normalise_city("GOA (DABOLIM, SOUTH GOA)") == "GOI"
    assert normalise_city("GOA (MOPA, NORTH GOA)") == "GOX"
    assert normalise_city("VISHAKHAPATNAM (VISAKHAPATNAM)") == "VTZ"


def test_qualifier_beats_parent():
    """'MUMBAI (NAVI MUMBAI)' is Navi Mumbai, not Mumbai. Getting this
    backwards would fold a distinct airport into BOM before the merge step
    can be reasoned about explicitly."""
    assert normalise_city("MUMBAI (NAVI MUMBAI)") == "NMI"
    assert CITY_MERGE["NMI"] == "BOM"        # merged later, deliberately


def test_unknown_city_returns_none_not_garbage():
    assert normalise_city("ATLANTIS") is None
    assert normalise_city("NOWHERE (SOMEWHERE)") is None
    assert normalise_city(None) is None
    assert normalise_city(123) is None


def _fixture() -> pd.DataFrame:
    return pd.DataFrame([
        # Year, Month, City1, City2, PaxToCity2, PaxFromCity2
        (2026, 5, "DELHI", "MUMBAI", 100_000, 90_000),
        (2026, 4, "DELHI", "MUMBAI", 80_000, 80_000),
        (2026, 5, "BENGALURU", "DELHI", 50_000, 60_000),
        (2026, 5, "DELHI", "GOA (DABOLIM, SOUTH GOA)", 10_000, 10_000),
        (2026, 5, "DELHI", "GOA (MOPA, NORTH GOA)", 15_000, 15_000),
        (2026, 5, "DELHI", "ATLANTIS", 5_000, 5_000),        # unmappable
        (2020, 5, "DELHI", "MUMBAI", 999_999, 999_999),      # outside window
    ], columns=["Year", "Month", "City1", "City2", "PaxToCity2", "PaxFromCity2"]
    ).assign(period=lambda d: pd.to_datetime(
        d.Year.astype(str) + "-" + d.Month.astype(str).str.zfill(2) + "-01"))


def test_directional_split():
    """One DGCA row becomes two directional routes with different weights."""
    basket, _ = build(_fixture(), months=3, top=10)
    r = basket.set_index("route")["pax"]
    assert r["DEL-BOM"] == 180_000      # 100k (May) + 80k (Apr)
    assert r["BOM-DEL"] == 170_000      # 90k + 80k
    assert r["DEL-BOM"] != r["BOM-DEL"]


def test_goa_airports_merge_into_one_route():
    """Mopa and Dabolim are one commercial city. Split, neither would make the
    basket; merged, Goa is correctly a top route."""
    basket, _ = build(_fixture(), months=3, top=10)
    routes = set(basket["route"])
    assert "DEL-GOX" not in routes
    assert basket.set_index("route").loc["DEL-GOI", "pax"] == 25_000  # 10k + 15k


def test_weights_sum_to_one():
    basket, _ = build(_fixture(), months=3, top=3)
    assert abs(basket["weight"].sum() - 1.0) < 1e-9
    # ...but market share does not, because the basket is a sample.
    assert basket["share_of_market"].sum() < 1.0


def test_window_excludes_old_months():
    basket, meta = build(_fixture(), months=3, top=10)
    assert meta["window_end"] == "2026-05"
    assert basket.set_index("route").loc["DEL-BOM", "pax"] < 999_999


def test_unmapped_cities_are_reported_not_hidden():
    _, meta = build(_fixture(), months=3, top=10)
    assert "ATLANTIS" in meta["unmapped_top"]
    assert meta["mapped_coverage"] < 1.0


def test_no_self_routes():
    df = _fixture()
    df.loc[len(df)] = (2026, 5, "MUMBAI", "MUMBAI (MUMBAI)", 1_000, 1_000,
                       pd.Timestamp("2026-05-01"))
    basket, _ = build(df, months=3, top=10)
    assert not (basket["origin"] == basket["destination"]).any()


def test_written_yaml_loads_into_the_collector(tmp_path):
    from apix.collector import index_config_from_basket

    basket, meta = build(_fixture(), months=3, top=4)
    out = tmp_path / "route_weights.yaml"
    write_yaml(basket, meta, out)
    doc = yaml.safe_load(out.read_text())

    assert doc["version"].startswith("dgca-2026-05")
    assert "DGCA" in doc["weight_source"]
    assert abs(sum(r["weight"] for r in doc["routes"]) - 1.0) < 1e-4

    cfg = index_config_from_basket({
        "routes": doc["routes"],
        "apw_weights": {1: 0.2, 7: 0.2, 15: 0.2, 30: 0.2, 45: 0.2},
        "collection": {},
    })
    assert abs(sum(cfg.normalised().route_weights.values()) - 1.0) < 1e-9


def test_real_basket_is_not_placeholder():
    """Guards against someone reverting basket.yaml to the invented weights."""
    b = yaml.safe_load(open("config/basket.yaml"))
    assert "PLACEHOLDER" not in b["weight_source"], "route weights are placeholders again"
    assert "DGCA" in b["weight_source"]
    assert abs(sum(r["weight"] for r in b["routes"]) - 1.0) < 0.01
    assert all("pax_12m" in r for r in b["routes"]), "weights lost their provenance"
