"""Replay source: serves quotes from an archived panel instead of the network.

Two jobs:
  * lets you develop and demo the whole pipeline with zero live requests, which
    is how you should be running 99% of the time;
  * makes the index reproducible — a backtest that hits live sites can never be
    re-run, and an official statistic has to be re-runnable on demand.

The archive is a parquet/CSV with the FareQuote column names.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Optional

import pandas as pd

from ..models import FareQuote
from .base import Source


class ReplaySource(Source):
    name = "replay:archive"
    collection_method = "replay"

    def __init__(self, policy, archive_path: str | Path, as_of: Optional[date] = None) -> None:
        super().__init__(policy)
        path = Path(archive_path)
        if path.suffix == ".parquet":
            self.df = pd.read_parquet(path)
        else:
            self.df = pd.read_csv(path)
        self.df["quote_ts"] = pd.to_datetime(self.df["quote_ts"], utc=True)
        self.df["departure_date"] = pd.to_datetime(self.df["departure_date"]).dt.date
        self.as_of = as_of

    def set_clock(self, as_of: date) -> None:
        """Pretend 'today' is `as_of`. Used to walk a backtest forward."""
        self.as_of = as_of

    def fetch(self, origin: str, destination: str, departure_date: date) -> List[FareQuote]:
        m = (
            (self.df["origin"] == origin)
            & (self.df["destination"] == destination)
            & (self.df["departure_date"] == departure_date)
        )
        if self.as_of is not None:
            m &= self.df["quote_ts"].dt.date == self.as_of
        rows = self.df[m]
        out: List[FareQuote] = []
        for r in rows.itertuples(index=False):
            out.append(
                FareQuote(
                    quote_ts=r.quote_ts.to_pydatetime().astimezone(timezone.utc),
                    origin=r.origin,
                    destination=r.destination,
                    departure_date=r.departure_date,
                    apw_days=int(r.apw_days),
                    carrier=r.carrier,
                    flight_number=_opt(getattr(r, "flight_number", None)),
                    cabin=getattr(r, "cabin", "ECONOMY"),
                    stops=int(getattr(r, "stops", 0) or 0),
                    duration_min=_optint(getattr(r, "duration_min", None)),
                    departure_time_local=_opt(getattr(r, "departure_time_local", None)),
                    total_fare=float(r.total_fare),
                    base_fare=_optfloat(getattr(r, "base_fare", None)),
                    taxes_and_fees=_optfloat(getattr(r, "taxes_and_fees", None)),
                    source=getattr(r, "source", self.name),
                    collection_method=self.collection_method,
                )
            )
        return out


def _opt(v):
    return None if v is None or (isinstance(v, float) and pd.isna(v)) or v != v else str(v)


def _optint(v):
    try:
        return None if v is None or v != v else int(v)
    except (TypeError, ValueError):
        return None


def _optfloat(v):
    try:
        return None if v is None or v != v else float(v)
    except (TypeError, ValueError):
        return None
