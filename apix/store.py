"""Supabase persistence.

The runner is ephemeral, so nothing survives unless it lands here. Two write
paths with different lifetimes:

  fare_quotes       every observation. Big, append-only, service-role only.
                    Kept so the index can be regenerated from raw inputs when
                    the cleaning logic changes.
  elementary_cells  one row per (date, route, apw, cabin). ~100 rows/day for a
                    20-route basket. This is what the daily index rebuild reads.

Rebuilding the headline index from cells rather than quotes is the difference
between pulling ~100 rows and ~200,000 per day of history. Only reach for
`load_quotes` when you have actually changed `normalize.py` and need a full
reprocess.

Everything is upsert-by-primary-key, so re-running a day is safe. That matters
more than it sounds: GitHub's scheduler drops runs, and the fix is always
"re-run yesterday", which must not double-count.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import date, datetime
from typing import Iterable, List, Optional

import pandas as pd

log = logging.getLogger("apix.store")

BATCH = 500          # supabase-py chokes well before this on wide rows
QUOTE_COLUMNS = [
    "quote_id", "quote_ts", "origin", "destination", "departure_date",
    "apw_days", "apw_bucket", "carrier", "flight_number", "cabin",
    "fare_family", "stops", "duration_min", "departure_time_local",
    "total_fare", "base_fare", "taxes_and_fees", "udf", "convenience_fee",
    "currency", "seats_available", "sold_out", "source", "collection_method",
    "drop_reason",
]


class Store:
    def __init__(self, url: Optional[str] = None, key: Optional[str] = None,
                 dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self.url = url or os.environ.get("SUPABASE_URL")
        self.key = key or os.environ.get("SUPABASE_SERVICE_KEY")
        self._client = None
        if not dry_run:
            if not (self.url and self.key):
                raise RuntimeError(
                    "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set. "
                    "Use dry_run=True for local development."
                )
            from supabase import create_client
            self._client = create_client(self.url, self.key)

    # ------------------------------------------------------------------ util
    def _upsert(self, table: str, rows: List[dict], on_conflict: str) -> int:
        if not rows:
            return 0
        if self.dry_run:
            log.info("[dry-run] would upsert %d rows into %s", len(rows), table)
            return len(rows)
        written = 0
        for i in range(0, len(rows), BATCH):
            chunk = rows[i:i + BATCH]
            self._client.table(table).upsert(chunk, on_conflict=on_conflict).execute()
            written += len(chunk)
            log.debug("%s: %d/%d", table, written, len(rows))
        log.info("%s: upserted %d rows", table, written)
        return written

    # ------------------------------------------------------------------ write
    def write_quotes(self, df: pd.DataFrame) -> int:
        if df.empty:
            log.warning("write_quotes called with an empty frame")
            return 0
        out = df.reindex(columns=QUOTE_COLUMNS).copy()
        out = _coerce_ints(out)
        return self._upsert("fare_quotes", _records(out), "quote_id")

    def write_cells(self, cells: pd.DataFrame, cabin: str = "ECONOMY") -> int:
        if cells.empty:
            return 0
        out = cells.copy()
        out["cabin"] = cabin
        cols = ["quote_date", "route", "apw", "cabin", "price",
                "n_quotes", "n_carriers", "imputed"]
        out = out.reindex(columns=cols)
        # reindex creates all-NaN object columns when the caller omitted one,
        # and fillna on an object column is the deprecated implicit downcast.
        # Convert the type first, then fill.
        out["n_quotes"] = pd.to_numeric(out["n_quotes"], errors="coerce").fillna(0).astype(int)
        out["n_carriers"] = pd.to_numeric(out["n_carriers"], errors="coerce").fillna(0).astype(int)
        out["imputed"] = out["imputed"].astype("boolean").fillna(False).astype(bool)
        return self._upsert("elementary_cells", _records(out),
                            "quote_date,route,apw,cabin")

    def write_index(self, result: dict, basket_id: Optional[str] = None) -> dict:
        counts = {}

        daily = result["daily"].copy()
        daily = daily.rename(columns={"n_quotes_cells": "n_cells"})
        daily["basket_id"] = basket_id
        # n_cells is a count that pandas carries as float. Postgres integer
        # rejects "140.0" — the same class of failure as apw_bucket, so it
        # goes through the same coercion.
        daily = _coerce_ints(daily)
        counts["apix_daily"] = self._upsert(
            "apix_daily",
            _records(daily.reindex(columns=[
                "quote_date", "apix", "dod_pct", "mom_pct", "yoy_pct",
                "pct_imputed", "n_cells", "basket_id"])),
            "quote_date",
        )

        rd = result["route_daily"].rename(columns={"index": "index_value"})
        counts["apix_route_daily"] = self._upsert(
            "apix_route_daily",
            _records(rd.reindex(columns=["quote_date", "route", "index_value", "pct_imputed"])),
            "quote_date,route",
        )

        m = result["monthly"].rename(columns={"quote_date": "month"})
        counts["apix_monthly"] = self._upsert(
            "apix_monthly",
            _records(m.reindex(columns=["month", "apix", "change_pct"])),
            "month",
        )
        return counts

    # ------------------------------------------------------------------ read
    def load_cells(self, since: Optional[date] = None,
                   cabin: str = "ECONOMY") -> pd.DataFrame:
        """Cells for an index rebuild. This is the cheap path."""
        if self.dry_run:
            return pd.DataFrame()
        q = self._client.table("elementary_cells").select("*").eq("cabin", cabin)
        if since:
            q = q.gte("quote_date", since.isoformat())
        return _paged(q)

    def load_quotes(self, start: date, end: date) -> pd.DataFrame:
        """Raw quotes for a full reprocess. Chunked by day — a year of a
        20-route basket is millions of rows and will time out in one call."""
        if self.dry_run:
            return pd.DataFrame()
        frames = []
        for d in pd.date_range(start, end, freq="D"):
            day = d.date().isoformat()
            q = (self._client.table("fare_quotes").select("*")
                 .eq("quote_date", day))
            frames.append(_paged(q))
        return pd.concat([f for f in frames if not f.empty], ignore_index=True) \
            if frames else pd.DataFrame()

    def collected_dates(self, limit_days: int = 400) -> List[date]:
        """Which days already have cells. Used to detect scheduler gaps."""
        if self.dry_run:
            return []
        r = (self._client.table("elementary_cells")
             .select("quote_date")
             .order("quote_date", desc=True)
             .limit(limit_days * 200).execute())
        return sorted({pd.to_datetime(x["quote_date"]).date() for x in (r.data or [])})

    # ------------------------------------------------------------------ ops
    def start_run(self, source: str) -> Optional[str]:
        if self.dry_run:
            return None
        r = self._client.table("collection_runs").insert(
            {"source": source, "status": "running"}).execute()
        return r.data[0]["id"] if r.data else None

    def finish_run(self, run_id: Optional[str], *, status: str,
                   quotes_stored: int = 0, requests_made: int = 0,
                   errors: Optional[list] = None) -> None:
        if self.dry_run or not run_id:
            return
        self._client.table("collection_runs").update({
            "finished_at": datetime.utcnow().isoformat(),
            "status": status,
            "quotes_stored": quotes_stored,
            "requests_made": requests_made,
            "errors": errors or [],
        }).eq("id", run_id).execute()

    def register_basket(self, basket: dict) -> Optional[str]:
        """Version the weight vector. Every published number must be traceable
        to the exact weights that produced it.

        Writes both `basket_version` (the whole config as JSON, for audit) and
        `route_weights` (one row per route, for querying). The JSON alone would
        be enough to reproduce a run, but you cannot join against it, and
        "which routes carried the most weight last March" is a question people
        actually ask.
        """
        if self.dry_run:
            return None
        payload = {
            "version": basket["version"],
            "effective_from": basket["effective_from"],
            "weight_source": basket["weight_source"],
            "payload": basket,
        }
        r = self._client.table("basket_version").upsert(
            payload, on_conflict="version").execute()
        basket_id = r.data[0]["id"] if r.data else None
        if not basket_id:
            return None

        rows = [
            {
                "basket_id": basket_id,
                "route": route["route"],
                "origin": route["origin"],
                "destination": route["destination"],
                "weight": float(route["weight"]),
            }
            for route in basket.get("routes", [])
        ]
        self._upsert("route_weights", rows, "basket_id,route")
        return basket_id


# Columns declared smallint/integer in sql/schema.sql. Pandas holds them as
# float64 the moment a single NaN appears — and `bucket_apw` returns Float64 by
# design, so apw_bucket is always a float here. Postgres rejects "45.0" for a
# smallint, which is what the first live write hit.
INT_COLUMNS = ("apw_days", "apw_bucket", "stops", "duration_min",
               "seats_available", "n_cells", "n_quotes", "n_carriers",
               "revision")


def _coerce_ints(df: pd.DataFrame) -> pd.DataFrame:
    """Round-trip integer-typed columns through a nullable Int64.

    Nullable so genuinely-missing values stay NULL rather than becoming 0 —
    a duration_min of 0 would be a claim about the flight, not an absence.
    """
    out = df.copy()
    for col in INT_COLUMNS:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").round().astype("Int64")
    return out


# ---------------------------------------------------------------- helpers
def _paged(query, page: int = 1000) -> pd.DataFrame:
    """PostgREST caps rows per response; walk the range header."""
    rows, offset = [], 0
    while True:
        r = query.range(offset, offset + page - 1).execute()
        batch = r.data or []
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return pd.DataFrame(rows)


def _records(df: pd.DataFrame) -> List[dict]:
    """DataFrame -> JSON-safe dicts. NaN is not valid JSON and psycopg will
    not coerce it; every one has to become None explicitly."""
    out = []
    for rec in df.to_dict(orient="records"):
        clean = {}
        for k, v in rec.items():
            if v is None or v is pd.NA or (isinstance(v, float) and math.isnan(v)):
                clean[k] = None
            elif isinstance(v, (pd.Timestamp, datetime)):
                clean[k] = v.isoformat()
            elif isinstance(v, date):
                clean[k] = v.isoformat()
            elif hasattr(v, "item"):          # numpy scalar
                val = v.item()
                clean[k] = None if isinstance(val, float) and math.isnan(val) else val
            elif isinstance(v, pd.Period):
                clean[k] = str(v)
            else:
                clean[k] = v
        out.append(clean)
    return out
