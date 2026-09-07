"""Convert the uploaded EaseMyTrip archive into the APIx quote schema.

Important provenance note, verified against the files: every row in
Clean_Dataset.csv was captured on a SINGLE collection date (2022-02-10),
covering departures from 2022-02-11 to 2022-03-31 with lead times of 1-49 days.

That makes it an excellent cross-section for lead-time elasticity and for
validating the cleaning code, and useless as a time series. Do not try to build
a 30-day backtest from it.

Usage:
    python scripts/ingest_kaggle_archive.py <airfare_dir> out/quotes_2022.parquet
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

CITY_TO_IATA = {
    "Delhi": "DEL", "New Delhi": "DEL", "Mumbai": "BOM", "Bangalore": "BLR",
    "Banglore": "BLR", "Bengaluru": "BLR", "Kolkata": "CCU", "Hyderabad": "HYD",
    "Chennai": "MAA",
}
AIRLINE_TO_IATA = {
    "SpiceJet": "SG", "AirAsia": "I5", "Vistara": "UK", "GO_FIRST": "G8",
    "GO FIRST": "G8", "Indigo": "6E", "IndiGo": "6E", "Air_India": "AI",
    "Air India": "AI", "Akasa Air": "QP", "Trujet": "2T", "StarAir": "S5",
}


def _align_dates(clean: pd.DataFrame, raw: pd.DataFrame) -> np.ndarray:
    """Clean_Dataset dropped ~108 rows but preserved order; walk both cursors."""
    out = np.empty(len(clean), dtype=object)
    j = 0
    prices_raw = raw["_p"].values
    prices_cl = clean["price"].values
    dates_raw = raw["date"].values
    for i in range(len(clean)):
        while j < len(prices_raw) and prices_raw[j] != prices_cl[i]:
            j += 1
        if j < len(prices_raw):
            out[i] = dates_raw[j]
            j += 1
    return out


def build(src: Path) -> pd.DataFrame:
    clean = pd.read_csv(src / "Clean_Dataset.csv", index_col=0)
    econ = pd.read_csv(src / "economy.csv", encoding="utf-8-sig")
    biz = pd.read_csv(src / "business.csv", encoding="utf-8-sig")
    for d in (econ, biz):
        d["_p"] = d["price"].astype(str).str.replace(",", "").astype(int)

    frames = []
    for cabin_label, raw, cabin in (("Economy", econ, "ECONOMY"),
                                    ("Business", biz, "BUSINESS")):
        c = clean[clean["class"] == cabin_label].reset_index(drop=True)
        c["departure_date"] = pd.to_datetime(
            pd.Series(_align_dates(c, raw)), format="%d-%m-%Y", errors="coerce"
        )
        c["cabin"] = cabin
        frames.append(c)

    df = pd.concat(frames, ignore_index=True).dropna(subset=["departure_date"])
    df["quote_ts"] = (df["departure_date"] - pd.to_timedelta(df["days_left"], unit="D")
                      ).dt.tz_localize(timezone.utc) + pd.Timedelta(hours=6)

    out = pd.DataFrame({
        "quote_ts": df["quote_ts"],
        "origin": df["source_city"].map(CITY_TO_IATA),
        "destination": df["destination_city"].map(CITY_TO_IATA),
        "departure_date": df["departure_date"].dt.date,
        "apw_days": df["days_left"].astype(int),
        "carrier": df["airline"].map(AIRLINE_TO_IATA).fillna("??"),
        "flight_number": df["flight"].str.replace("-", "", regex=False),
        "cabin": df["cabin"],
        "stops": df["stops"].map({"zero": 0, "one": 1, "two_or_more": 2}).fillna(0).astype(int),
        "duration_min": (df["duration"] * 60).round().astype(int),
        "departure_time_local": df["departure_time"],
        "total_fare": df["price"].astype(float),
        "base_fare": np.nan,
        "taxes_and_fees": np.nan,
        "source": "replay:easemytrip_2022",
        "collection_method": "replay",
    }).dropna(subset=["origin", "destination"])

    return out


if __name__ == "__main__":
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    dst.parent.mkdir(parents=True, exist_ok=True)
    q = build(src)
    q.to_parquet(dst, index=False)
    print(f"wrote {len(q):,} quotes -> {dst}")
    print("collection dates:", sorted({str(d) for d in pd.to_datetime(q['quote_ts']).dt.date})[:5])
    print("routes:", q['origin'].str.cat(q['destination'], sep='-').nunique())
