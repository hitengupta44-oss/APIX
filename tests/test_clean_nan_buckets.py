"""Regression tests for unbucketed lead times in the cleaner.

`apw_bucket` is NaN whenever an observed lead time falls outside the tolerance
of every target window — routine with a cached source whose quotes carry a
cache-age adjustment. pandas 2 drops NaN group keys by default, so the outlier
transform returned fewer rows than the frame and the comparison raised
"Can only compare identically-labeled Series objects".

It crashed in CI and not locally because the two run different pandas majors,
which is exactly the kind of bug that only appears once something real is
running on a schedule.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from apix.normalize import clean, run_pipeline


def frame(n_bucketed: int, n_unbucketed: int, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = n_bucketed + n_unbucketed
    return pd.DataFrame({
        "quote_id": [str(i) for i in range(n)],
        "quote_ts": pd.Timestamp("2026-09-04T06:00:00Z"),
        "quote_date": [pd.Timestamp("2026-09-04").date()] * n,
        "origin": ["DEL"] * n,
        "destination": ["BOM"] * n,
        "departure_date": [pd.Timestamp("2026-09-11").date()] * n,
        "apw_days": [7] * n,
        "apw_bucket": [7.0] * n_bucketed + [np.nan] * n_unbucketed,
        "carrier": ["6E"] * n,
        # Distinct flight numbers: identical ones are collapsed by the
        # cross-source dedup step, which would mask what these tests check.
        "flight_number": [f"6E{i:03d}" for i in range(n)],
        "cabin": ["ECONOMY"] * n,
        "total_fare": rng.lognormal(8.6, 0.2, n),
        "base_fare": np.nan, "taxes_and_fees": np.nan,
        "udf": np.nan, "convenience_fee": np.nan,
        "currency": ["INR"] * n, "sold_out": [False] * n,
        "stops": [0] * n, "source": ["t"] * n,
        "collection_method": ["cached_api"] * n,
    })


def test_mixed_bucketed_and_unbucketed_does_not_crash():
    out = clean(frame(30, 10))
    assert len(out) == 40
    assert "drop_reason" in out


def test_all_unbucketed_does_not_crash():
    """Every quote outside tolerance — a plausible day on a cached feed."""
    out = clean(frame(0, 25))
    assert len(out) == 25


def test_unbucketed_rows_are_kept_not_silently_dropped():
    """A quote with no bucket is still an observation. It is excluded at the
    index stage, where that decision is explicit, not lost during cleaning."""
    out = clean(frame(20, 8))
    unbucketed = out[out["apw_bucket"].isna()]
    assert len(unbucketed) == 8
    assert unbucketed["drop_reason"].isna().all()


def test_outlier_detection_still_works_alongside_nan_buckets():
    df = frame(30, 10, seed=3)
    df.loc[df.index[:3], "total_fare"] = 900_000.0
    out = clean(df)
    flagged = out.loc[df.index[:3], "drop_reason"]
    assert flagged.notna().all(), "extreme fares should still be caught"


def test_outliers_within_the_nan_group_are_evaluated():
    """NaN buckets form their own group rather than being skipped, so a wild
    fare among them is still caught."""
    df = frame(5, 20, seed=7)
    df.loc[df.index[-2:], "total_fare"] = 900_000.0
    out = clean(df)
    assert out.loc[df.index[-2:], "drop_reason"].notna().all()


def test_full_pipeline_on_a_frame_with_unbucketed_quotes():
    from apix.models import FareQuote
    from datetime import date, datetime, timezone

    quotes = []
    for i, apw in enumerate([7, 7, 7, 22, 22]):   # 22 is >2 days from any window
        quotes.append(FareQuote(
            quote_ts=datetime(2026, 9, 4, 6, tzinfo=timezone.utc),
            origin="DEL", destination="BOM",
            departure_date=date(2026, 9, 4 + apw), apw_days=apw,
            carrier="6E", flight_number=f"6E{i}",
            total_fare=5000.0 + i * 100, source="t",
        ))
    out = run_pipeline(quotes)
    assert len(out) == 5
    assert out["apw_bucket"].isna().sum() == 2
