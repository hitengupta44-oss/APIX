"""Cleaning pipeline: raw quotes -> analysis-ready panel.

Order matters. Each step is separately auditable because NSO will ask "how many
observations did you drop and why" and the honest answer has to be a table, not
a shrug. Every dropped row is tagged rather than deleted.
"""
from __future__ import annotations

import logging
from typing import Iterable, List

import numpy as np
import pandas as pd

from .models import FareQuote

log = logging.getLogger("apix.normalize")

# Statutory / quasi-statutory components as of FY25-26. Move to config and
# version them — when UDF changes at an airport the whole back-series shifts.
DEFAULT_UDF_BY_AIRPORT = {
    "DEL": 141.0, "BOM": 187.0, "BLR": 306.0, "HYD": 281.0,
    "MAA": 152.0, "CCU": 156.0, "COK": 300.0, "AMD": 155.0,
}
AVIATION_SECURITY_FEE = 236.0   # ASF, per departing domestic pax
GST_DOMESTIC_ECONOMY = 0.05
GST_DOMESTIC_PREMIUM = 0.12


def quotes_to_frame(quotes: Iterable[FareQuote]) -> pd.DataFrame:
    rows = [q.to_row() for q in quotes]
    if not rows:
        return pd.DataFrame(columns=list(FareQuote.__annotations__) + ["route", "quote_id"])
    df = pd.DataFrame(rows)
    df["quote_ts"] = pd.to_datetime(df["quote_ts"], utc=True)
    df["quote_date"] = df["quote_ts"].dt.date
    df["departure_date"] = pd.to_datetime(df["departure_date"]).dt.date
    return df


def clean(df: pd.DataFrame, *, iqr_k: float = 3.0, min_fare: float = 800.0,
          max_fare: float = 250_000.0) -> pd.DataFrame:
    """Return the frame with a `drop_reason` column; keep everything."""
    if df.empty:
        return df.assign(drop_reason=pd.Series(dtype=object))

    df = df.copy()
    df["drop_reason"] = None

    def mark(mask, reason):
        m = mask & df["drop_reason"].isna()
        df.loc[m, "drop_reason"] = reason

    # 1. Structural invalidity.
    mark(df["total_fare"].isna(), "missing_fare")
    mark(df["sold_out"].fillna(False).astype(bool), "sold_out")
    mark(df["total_fare"] < min_fare, "implausibly_low")
    mark(df["total_fare"] > max_fare, "implausibly_high")
    mark(df["currency"].ne("INR"), "non_inr")

    # 2. Duplicates. Same product, same day, same source -> keep the first.
    dup = df.duplicated(subset=["quote_id"], keep="first")
    mark(dup, "duplicate")

    # 3. Cross-source duplication: the same physical flight quoted by an
    #    airline site and three OTAs is one product, not four observations.
    #    Collapse to the median across sources so no OTA gets extra weight.
    live = df["drop_reason"].isna()
    key = ["quote_date", "origin", "destination", "departure_date",
           "carrier", "flight_number", "cabin"]
    if live.any():
        keep_idx = (
            df[live]
            .assign(_absdev=lambda d: (d["total_fare"] - d.groupby(key)["total_fare"].transform("median")).abs())
            .sort_values("_absdev")
            .groupby(key, dropna=False)
            .head(1)
            .index
        )
        redundant = live & ~df.index.isin(keep_idx)
        mark(redundant, "cross_source_duplicate")

    # 4. Outliers, within (route x apw x cabin) on a given day, on logs.
    #    Fares are right-skewed, so trimming in levels throws away real peak
    #    pricing. Logs + IQR keeps genuine surge but removes scrape artefacts.
    live = df["drop_reason"].isna()
    if live.sum() > 0:
        sub = df.loc[live].copy()
        sub["_log"] = np.log(sub["total_fare"])

        # dropna=False is essential. `apw_bucket` is NaN whenever an observed
        # lead time falls outside the tolerance of any target window, and
        # pandas 2 drops NaN group keys by default — so transform returns fewer
        # rows than `sub`, and comparing the two raises "Can only compare
        # identically-labeled Series objects". pandas 3 aligns instead, so this
        # crashed only in CI and only on the days that happened to contain an
        # unbucketed quote.
        key = ["quote_date", "origin", "destination", "cabin"]
        key.insert(3, "apw_bucket" if "apw_bucket" in sub.columns else "apw_days")
        g = sub.groupby(key, dropna=False)["_log"]

        q1 = g.transform(lambda s: s.quantile(0.25))
        q3 = g.transform(lambda s: s.quantile(0.75))
        n = g.transform("size")
        # Reindex defensively so a future pandas change cannot reintroduce the
        # misalignment silently.
        q1, q3, n = (x.reindex(sub.index) for x in (q1, q3, n))
        iqr = q3 - q1

        out = (n >= 5) & (
            (sub["_log"] < q1 - iqr_k * iqr) | (sub["_log"] > q3 + iqr_k * iqr)
        )
        out = out.fillna(False).astype(bool)
        mark(df.index.isin(sub.index[out]), "outlier_iqr")

    kept = df["drop_reason"].isna().sum()
    log.info("clean: kept %d/%d (%.1f%%)", kept, len(df), 100 * kept / max(len(df), 1))
    return df


def decompose_fare(df: pd.DataFrame,
                   udf_table: dict | None = None) -> pd.DataFrame:
    """Split total into base / tax / statutory where the source did not.

    CPI convention is that the index tracks what the consumer actually pays, so
    APIx headline uses `total_fare`. The decomposition exists so you can publish
    an ex-tax series alongside it and answer "how much of the move was policy
    versus the airline".
    """
    udf_table = udf_table or DEFAULT_UDF_BY_AIRPORT
    df = df.copy()

    # to_numeric first: these arrive as all-NaN object columns when no source
    # reports a fee breakdown, and fillna on an object column is the deprecated
    # implicit downcast.
    udf_reported = pd.to_numeric(df["udf"], errors="coerce")
    udf_default = pd.to_numeric(df["origin"].map(udf_table), errors="coerce").fillna(160.0)
    df["udf_est"] = udf_reported.where(udf_reported.notna(), udf_default)
    df["asf_est"] = AVIATION_SECURITY_FEE
    gst_rate = np.where(df["cabin"].eq("ECONOMY"), GST_DOMESTIC_ECONOMY, GST_DOMESTIC_PREMIUM)

    base_reported = pd.to_numeric(df["base_fare"], errors="coerce")
    have_base = base_reported.notna()
    # Reported base wins. Otherwise back it out of the total.
    convenience = pd.to_numeric(df["convenience_fee"], errors="coerce").fillna(0.0)
    residual = df["total_fare"] - df["udf_est"] - df["asf_est"] - convenience
    implied_base = residual / (1.0 + gst_rate)

    df["base_fare_final"] = base_reported.where(have_base, implied_base).clip(lower=0)
    df["gst_est"] = df["base_fare_final"] * gst_rate
    df["taxes_total_est"] = (df["total_fare"] - df["base_fare_final"]).clip(lower=0)
    df["tax_share"] = df["taxes_total_est"] / df["total_fare"]
    return df


def bucket_apw(days: pd.Series, windows=(1, 7, 15, 30, 45), tol: int = 2) -> pd.Series:
    """Snap an observed lead time onto the nearest mandated APW window.

    Real collection drifts (a job runs late, a date is a Sunday). Snapping with
    a tolerance keeps the cell populated instead of silently losing the day.
    """
    arr = np.asarray(days, dtype=float)
    w = np.asarray(windows, dtype=float)
    idx = np.abs(arr[:, None] - w[None, :]).argmin(axis=1)
    nearest = w[idx]
    ok = np.abs(arr - nearest) <= tol
    return pd.Series(np.where(ok, nearest, np.nan), index=days.index).astype("Float64")


def run_pipeline(quotes: Iterable[FareQuote]) -> pd.DataFrame:
    df = quotes_to_frame(quotes)
    if df.empty:
        return df
    df["apw_bucket"] = bucket_apw(df["apw_days"])
    df = clean(df)
    df = decompose_fare(df)
    return df


def drop_report(df: pd.DataFrame) -> pd.DataFrame:
    """The table NSO will ask for."""
    if df.empty:
        return pd.DataFrame(columns=["drop_reason", "n", "pct"])
    r = df["drop_reason"].fillna("kept").value_counts().rename_axis("drop_reason").reset_index(name="n")
    r["pct"] = (100 * r["n"] / len(df)).round(2)
    return r
