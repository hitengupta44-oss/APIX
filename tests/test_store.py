"""Tests for the persistence layer and the two index-rebuild paths.

The critical one is `test_cell_path_matches_quote_path`. Collection stores
elementary cells and the nightly job rebuilds from those; a full reprocess
rebuilds from raw quotes. If the two ever disagree, the index silently changes
value depending on which job last ran, and you will not notice until someone
compares two exports.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from apix.index import build_index, elementary_aggregates, impute_cells
from apix.store import Store, _records
from scripts.rebuild import _aggregate_cells
from tests.test_index import CFG, synth


def test_cell_path_matches_quote_path():
    df = synth(days=45, drift=0.0012)
    from_quotes = build_index(df, CFG)

    # Simulate the storage round-trip: only observed cells are persisted.
    cells = elementary_aggregates(df, CFG).dropna(subset=["price"])
    stored = cells[["quote_date", "route", "apw", "price", "n_quotes", "n_carriers"]]
    from_cells = _aggregate_cells(impute_cells(stored, CFG), CFG)

    np.testing.assert_allclose(
        from_quotes["daily"]["apix"].values,
        from_cells["daily"]["apix"].values,
        rtol=1e-9,
        err_msg="cell rebuild and quote rebuild disagree — the nightly job and "
                "a reprocess would publish different numbers",
    )


def test_cell_path_survives_a_missing_day():
    df = synth(days=30)
    cells = elementary_aggregates(df, CFG).dropna(subset=["price"])
    gap = date(2026, 4, 12)
    cells = cells[cells["quote_date"] != gap]        # scheduler dropped a run

    res = _aggregate_cells(impute_cells(cells, CFG), CFG)
    days = pd.to_datetime(res["daily"]["quote_date"]).dt.date.tolist()
    assert gap not in days                            # the day is absent...
    assert len(days) == 29                            # ...and nothing else broke
    assert res["daily"]["apix"].notna().all()


def test_integer_columns_are_not_sent_as_floats():
    """Postgres smallint rejects "45.0".

    `bucket_apw` returns Float64 by design, and pandas floats any int column
    the moment one NaN appears — so every apw_bucket reaching the database was
    a float. This only surfaced on the first live write, which is exactly the
    kind of failure a test should catch instead.
    """
    from apix.store import INT_COLUMNS, _coerce_ints

    df = pd.DataFrame([{
        "quote_id": "abc", "apw_days": 7.0, "apw_bucket": 45.0,
        "stops": 0.0, "duration_min": 125.0, "seats_available": np.nan,
        "total_fare": 5432.5,
    }])
    out = _coerce_ints(df)
    recs = _records(out)[0]

    for col in ("apw_days", "apw_bucket", "stops", "duration_min"):
        assert isinstance(recs[col], int), f"{col} is {type(recs[col])}, not int"
        assert "." not in str(recs[col])
    # Missing stays NULL. Coercing to 0 would assert something false about the
    # flight rather than admit the value is absent.
    assert recs["seats_available"] is None
    # Money must stay a float.
    assert recs["total_fare"] == 5432.5
    json.dumps(recs)


def test_coerce_ints_survives_a_full_pipeline_frame():
    """Run a real cleaned frame through, since that is where Float64 appears."""
    from apix.store import _coerce_ints

    df = synth(days=2)
    out = _coerce_ints(df.reindex(columns=list(df.columns)))
    assert str(out["apw_bucket"].dtype) == "Int64"
    for rec in _records(out)[:20]:
        if rec.get("apw_bucket") is not None:
            assert isinstance(rec["apw_bucket"], int)


def test_write_cells_handles_a_frame_missing_columns():
    """reindex fabricates all-NaN object columns for anything the caller left
    out. Filling those directly is the deprecated implicit downcast, and the
    fallout is silent: `imputed` stays object dtype and the .mean() behind
    pct_imputed stops working."""
    import warnings

    cells = pd.DataFrame([
        {"quote_date": date(2026, 9, 8), "route": "DEL-BOM", "apw": 7,
         "price": 5432.0, "n_quotes": 1, "n_carriers": 1},
    ])  # no `imputed` column at all
    s = Store(dry_run=True)
    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        assert s.write_cells(cells) == 1


def test_index_tables_send_counts_as_integers():
    """Postgres integer rejects "140.0".

    n_cells is a count that pandas carries as float, and it reached the
    database unconverted — the same failure as apw_bucket, one table further
    along. Both now go through the same coercion.
    """
    from apix.store import _coerce_ints

    res = build_index(synth(days=20), CFG)
    daily = res["daily"].rename(columns={"n_quotes_cells": "n_cells"})
    recs = _records(_coerce_ints(daily))
    for r in recs:
        assert isinstance(r["n_cells"], int), f"n_cells is {type(r['n_cells'])}"
    json.dumps(recs)


def test_pct_change_does_not_invent_a_flat_day():
    """A gap in the series is missing data, not a zero change.

    pandas 2 pad-filled by default, carrying the last value forward and
    reporting 0.00% — a number that reads as "prices held steady" when the
    truth is "we did not collect". On an index feeding monetary policy that is
    the wrong error to make quietly. pandas 3 made fill_method=None the
    default, so this asserts the property we depend on rather than the old
    behaviour, and holds on both versions.
    """
    s = pd.Series([100.0, np.nan, 104.0])
    honest = s.pct_change(fill_method=None)
    assert pd.isna(honest.iloc[1]), "a missing day must not report a change"
    assert pd.isna(honest.iloc[2]), "the day after a gap has no known baseline"

    clean = pd.Series([100.0, 102.0, 104.0]).pct_change(fill_method=None)
    assert clean.iloc[1] == pytest.approx(0.02)


def test_records_emit_valid_json():
    """NaN is not JSON and PostgREST rejects it. Every NaN must become null."""
    res = build_index(synth(days=20), CFG)
    for frame in (res["daily"], res["monthly"], res["route_daily"]):
        recs = _records(frame)
        json.dumps(recs)                              # raises on NaN/NaT
        assert not any(
            isinstance(v, float) and math.isnan(v)
            for r in recs for v in r.values()
        )


def test_records_serialise_dates_and_numpy():
    df = pd.DataFrame([{
        "d": date(2026, 4, 1),
        "ts": pd.Timestamp("2026-04-01T06:00:00Z"),
        "n": np.int64(7),
        "f": np.float64(1.5),
        "missing": np.nan,
    }])
    r = _records(df)[0]
    assert r["d"] == "2026-04-01"
    assert r["ts"].startswith("2026-04-01")
    assert r["n"] == 7 and isinstance(r["n"], int)
    assert r["f"] == 1.5
    assert r["missing"] is None
    json.dumps(r)


def test_dry_run_store_needs_no_credentials(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    s = Store(dry_run=True)
    assert s.write_quotes(pd.DataFrame()) == 0
    with pytest.raises(RuntimeError, match="SUPABASE_URL"):
        Store(dry_run=False)


def test_quote_columns_match_schema():
    """The upsert column list must match sql/schema.sql, or rows silently
    lose fields on write."""
    from pathlib import Path

    from apix.store import QUOTE_COLUMNS

    sql = Path("sql/schema.sql").read_text()
    body = sql.split("create table if not exists fare_quotes (", 1)[1].split(");", 1)[0]
    declared = {
        line.strip().split()[0]
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith(("primary", "--"))
    }
    missing = set(QUOTE_COLUMNS) - declared
    assert not missing, f"columns written but not declared in schema: {missing}"


def test_upsert_is_idempotent_on_quote_id():
    """Re-running a day must not duplicate. Same inputs -> same quote_ids."""
    a = synth(days=2, seed=3)
    b = synth(days=2, seed=3)
    from apix.models import FareQuote
    mk = lambda r: FareQuote(
        quote_ts=datetime(2026, 4, 1, 6, tzinfo=timezone.utc),
        origin=r.origin, destination=r.destination,
        departure_date=r.departure_date, apw_days=int(r.apw_days),
        carrier=r.carrier, flight_number=r.flight_number, source="test",
        total_fare=float(r.total_fare),
    )
    ids_a = {mk(r).quote_id for r in a.head(200).itertuples()}
    ids_b = {mk(r).quote_id for r in b.head(200).itertuples()}
    assert ids_a == ids_b