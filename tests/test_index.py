"""Index tests.

These check statistical *properties*, not golden numbers. A CPI-style index has
identities that must hold regardless of the data, and those are what protect
you when someone changes the cleaning code six weeks from now.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from apix.index import IndexConfig, build_index, elementary_aggregates
from apix.models import FareQuote
from apix.normalize import bucket_apw, clean, decompose_fare, run_pipeline

ROUTES = [("DEL", "BOM"), ("DEL", "BLR"), ("BOM", "BLR")]
CARRIERS = ["6E", "AI", "SG", "QP", "UK"]
WINDOWS = [1, 7, 15, 30, 45]


def synth(days: int = 60, drift: float = 0.0, seed: int = 7,
          start: date = date(2026, 4, 1)) -> pd.DataFrame:
    """Synthetic panel with a known daily drift, so we can assert recovery."""
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(days):
        qd = start + timedelta(days=d)
        for (o, dd) in ROUTES:
            for w in WINDOWS:
                base = {1: 9000, 7: 7000, 15: 5200, 30: 4400, 45: 4100}[w]
                base *= (1.0 + drift) ** d
                for c in CARRIERS:
                    for _ in range(3):
                        rows.append({
                            "quote_ts": datetime(qd.year, qd.month, qd.day, 6, tzinfo=timezone.utc),
                            "quote_date": qd,
                            "origin": o, "destination": dd,
                            "departure_date": qd + timedelta(days=w),
                            "apw_days": w, "apw_bucket": float(w),
                            "carrier": c,
                            "flight_number": f"{c}{rng.integers(100, 999)}",
                            "cabin": "ECONOMY", "stops": 0,
                            "total_fare": float(base * rng.lognormal(0, 0.18)),
                            "base_fare": np.nan, "taxes_and_fees": np.nan,
                            "udf": np.nan, "convenience_fee": np.nan,
                            "currency": "INR", "sold_out": False,
                            "source": "test", "collection_method": "replay",
                        })
    df = pd.DataFrame(rows)
    df["quote_id"] = df.index.astype(str)
    return clean(df)


CFG = IndexConfig(
    route_weights={"DEL-BOM": 0.5, "DEL-BLR": 0.3, "BOM-BLR": 0.2},
    apw_weights={1: 0.12, 7: 0.23, 15: 0.27, 30: 0.24, 45: 0.14},
    base_period="2026-04",
)


def test_no_silent_downcasting_in_the_pipeline():
    """pandas removed implicit downcasting on fillna/ffill.

    Where it bites here is `imputed`: reindexing introduces NaN, and if the
    column is left as object dtype the .mean() that produces pct_imputed
    silently stops working. Asserting the dtype directly catches a regression
    without depending on `future.no_silent_downcasting`, which is itself
    deprecated now that the behaviour is the default.
    """
    res = build_index(synth(days=30), CFG)
    assert res["cells"]["imputed"].dtype == bool
    assert res["daily"]["pct_imputed"].notna().all()
    assert res["daily"]["pct_imputed"].between(0, 100).all()


def test_single_quote_cells_survive_when_configured():
    """A cheapest-fare-only source gives exactly one quote per cell.

    With the default min_quotes_per_cell of 3 every cell is blanked and the
    index has nothing to aggregate — 89 quotes produced 0 cells on the first
    live Travelpayouts run. Setting it to 1 is a deliberate weakening for
    thin sources, so it is pinned here.
    """
    df = synth(days=5)
    live = df["drop_reason"].isna()
    # Keep one quote per (date, route, apw), mimicking a cheapest-only feed.
    keep = (df[live]
            .groupby(["quote_date", "route" if "route" in df else "origin",
                      "apw_bucket"], dropna=False)
            .head(1).index)
    thin = df.copy()
    thin.loc[live & ~df.index.isin(keep), "drop_reason"] = "trimmed_for_test"

    strict = IndexConfig(CFG.route_weights, CFG.apw_weights,
                         base_period="2026-04", min_quotes_per_cell=3)
    assert elementary_aggregates(thin, strict)["price"].isna().all()

    loose = IndexConfig(CFG.route_weights, CFG.apw_weights,
                        base_period="2026-04", min_quotes_per_cell=1)
    cells = elementary_aggregates(thin, loose)
    assert cells["price"].notna().any()


def test_base_period_error_lists_available_months():
    """A base period with no data is a config error, not a data error, and the
    message has to say which months exist — otherwise you are guessing at the
    one setting every index value is measured against."""
    df = synth(days=20)                      # April 2026 only
    cfg = IndexConfig(CFG.route_weights, CFG.apw_weights,
                      base_period="2020-01", min_quotes_per_cell=1)
    with pytest.raises(ValueError, match="2026-04"):
        build_index(df, cfg)


def test_basket_base_period_matches_the_data_era():
    """basket.yaml shipped with 2026-04 from the synthetic-data era, which made
    the first real rebuild fail. Pin it to a plausible collection month."""
    import yaml
    b = yaml.safe_load(open("config/basket.yaml"))
    bp = b.get("base_period")
    assert bp is None or bp >= "2026-09", (
        f"base_period {bp!r} predates collection; every index value would be "
        "measured against a month with no data")


def test_single_day_of_data_produces_a_real_index():
    """Day one is the hardest case and the first one you hit in production.

    impute_cells reindexes to the full route x window grid, so unobserved
    combinations appear as NaN. With no history there is nothing to carry
    forward and no anchor for class imputation, so they stay NaN — and feeding
    those into a weighted average produced a null apix, which violates the
    not-null constraint on apix_daily.
    """
    df = synth(days=1)
    cfg = IndexConfig(CFG.route_weights, CFG.apw_weights,
                      base_period="2026-04", min_quotes_per_cell=1)
    res = build_index(df, cfg)
    assert len(res["daily"]) == 1
    assert res["daily"]["apix"].notna().all()
    assert res["daily"]["apix"].iloc[0] == pytest.approx(100.0, abs=0.01)


def test_partial_basket_does_not_null_the_index():
    """Most routes missing is normal on a thin source. The index should be
    computed from what exists, not nulled because the grid has holes."""
    df = synth(days=3)
    live = df["drop_reason"].isna()
    drop = live & df["origin"].eq("DEL") & df["destination"].eq("BLR")
    df.loc[drop, "drop_reason"] = "not_collected"

    cfg = IndexConfig(CFG.route_weights, CFG.apw_weights,
                      base_period="2026-04", min_quotes_per_cell=1)
    res = build_index(df, cfg)
    assert res["daily"]["apix"].notna().all()
    assert "DEL-BLR" not in set(res["cells"]["route"])


def test_empty_basket_raises_rather_than_writing_nulls():
    df = synth(days=2)
    df["drop_reason"] = "everything_dropped"
    cfg = IndexConfig(CFG.route_weights, CFG.apw_weights, min_quotes_per_cell=1)
    with pytest.raises(ValueError):
        build_index(df, cfg)


def test_basket_allows_thin_cells():
    """The shipped config must not silently blank every cell again."""
    import yaml
    b = yaml.safe_load(open("config/basket.yaml"))
    assert b["collection"]["min_quotes_per_cell"] <= 1


def test_base_period_is_100():
    res = build_index(synth(days=45), CFG)
    apr = res["daily"][pd.to_datetime(res["daily"]["quote_date"]).dt.strftime("%Y-%m") == "2026-04"]
    # Mean over the base month must sit at 100 by construction.
    assert abs(apr["apix"].mean() - 100.0) < 1.5


def test_flat_prices_give_flat_index():
    res = build_index(synth(days=40, drift=0.0), CFG)
    assert res["daily"]["apix"].std() < 3.0


def test_known_drift_is_recovered():
    daily_drift = 0.002          # 0.2% a day
    res = build_index(synth(days=60, drift=daily_drift), CFG)
    d = res["daily"]
    implied = (d["apix"].iloc[-1] / d["apix"].iloc[0]) ** (1 / (len(d) - 1)) - 1
    assert abs(implied - daily_drift) < 0.0006


def test_scale_invariance():
    """Doubling every fare must double the index, not shift it."""
    a = synth(days=30)
    b = a.copy()
    b["total_fare"] *= 2
    ra, rb = build_index(a, CFG), build_index(b, CFG)
    # Both rebase to 100, so the *levels* must be identical.
    np.testing.assert_allclose(ra["daily"]["apix"], rb["daily"]["apix"], rtol=1e-9)


def test_jevons_not_carli():
    """Elementary aggregate must be the geometric, not arithmetic, mean."""
    df = synth(days=2)
    ea = elementary_aggregates(df, CFG)
    live = df[df["drop_reason"].isna()]
    cell = live[(live["quote_date"] == live["quote_date"].min())
                & (live["origin"] == "DEL") & (live["destination"] == "BOM")
                & (live["apw_bucket"] == 15)]
    per_carrier = cell.groupby("carrier")["total_fare"].min()
    geo = float(np.exp(np.log(per_carrier).mean()))
    got = ea[(ea["route"] == "DEL-BOM") & (ea["apw"] == 15)]["price"].iloc[0]
    assert abs(got - geo) < 1e-6
    assert geo < per_carrier.mean()      # Jevons sits below Carli


def test_weights_actually_bind():
    """Move one route only; the index must move by roughly its weight share.

    The shock is placed in May, outside the April base period. If you shock
    inside the base month the base prices absorb half of it and the lift comes
    out at ~2.4% instead of ~5% — correct index behaviour, and a good reminder
    that a basket revision inside a shock window will understate it.
    """
    shock_from = date(2026, 5, 1)
    a = synth(days=60)
    b = a.copy()
    m = (b["origin"] == "DEL") & (b["destination"] == "BOM")
    b.loc[m & (b["quote_date"] >= shock_from), "total_fare"] *= 1.10
    ra, rb = build_index(a, CFG), build_index(b, CFG)
    late = pd.to_datetime(rb["daily"]["quote_date"]).dt.date >= shock_from
    lift = (rb["daily"].loc[late, "apix"].mean() / ra["daily"].loc[late, "apix"].mean()) - 1
    assert 0.045 < lift < 0.055         # ~0.5 weight x 10%


def test_missing_cells_are_imputed_not_dropped():
    df = synth(days=30)
    hole = (df["quote_date"] == date(2026, 4, 10)) & (df["apw_bucket"] == 7)
    df.loc[hole, "drop_reason"] = "sold_out"
    res = build_index(df, CFG)
    d = res["daily"]
    assert len(d) == 30                                  # no day vanishes
    assert d.loc[pd.to_datetime(d["quote_date"]).dt.date == date(2026, 4, 10),
                 "pct_imputed"].iloc[0] > 0


def test_outliers_removed_on_logs():
    df = synth(days=5)
    n_before = df["drop_reason"].isna().sum()
    df.loc[df.index[:20], "total_fare"] = 500_000
    df["drop_reason"] = None
    df2 = clean(df)
    assert df2.loc[df.index[:20], "drop_reason"].notna().all()


def test_fare_decomposition_adds_up():
    df = decompose_fare(synth(days=3))
    live = df[df["drop_reason"].isna()]
    total = live["base_fare_final"] + live["taxes_total_est"]
    np.testing.assert_allclose(total, live["total_fare"], rtol=1e-6)
    assert (live["tax_share"].between(0.02, 0.45)).mean() > 0.95


def test_apw_bucketing_tolerance():
    s = pd.Series([1, 2, 8, 14, 16, 22, 31, 44, 46])
    got = bucket_apw(s, tol=2)
    assert list(got.dropna().astype(int)) == [1, 1, 7, 15, 15, 30, 45, 45]
    assert pd.isna(got.iloc[5])      # 22 is >2 days from any window


def test_quote_id_is_stable_and_deduplicating():
    kw = dict(quote_ts=datetime(2026, 4, 1, 6, tzinfo=timezone.utc),
              origin="del", destination="BOM", departure_date=date(2026, 4, 8),
              apw_days=7, carrier="6e", flight_number="6E2034", total_fare=5000.0,
              source="test")
    a, b = FareQuote(**kw), FareQuote(**kw)
    assert a.quote_id == b.quote_id
    assert a.origin == "DEL" and a.carrier == "6E"
    c = FareQuote(**{**kw, "flight_number": "6E2035"})
    assert c.quote_id != a.quote_id


def test_empty_input_raises_clearly():
    with pytest.raises(ValueError):
        build_index(pd.DataFrame(columns=["drop_reason", "cabin", "apw_bucket"]), CFG)