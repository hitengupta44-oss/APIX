"""Capacity context for airfare movements.

The question this answers: when APIx rises, is it demand or is it seats?

A fare index on its own cannot tell those apart, and it is the first thing an
economist will ask about any spike you report. DGCA's monthly carrier
statistics give ASK (available seat kilometres) — the supply side — so a rise
in fares against falling ASK is a capacity story, and a rise against flat or
growing ASK is a demand story.

Two limits to state plainly wherever you use this:

1. **Granularity mismatch.** DGCA carrier statistics are national and monthly.
   APIx is route-level and daily. So this contextualises the national monthly
   picture; it cannot explain why DEL-BOM moved on a Tuesday. Do not present a
   national ASK figure as though it explained a route-level move.

2. **Load factor is the sharper signal.** ASK falling with load factor rising
   means the same passengers are chasing fewer seats, which is unambiguously
   upward pressure on fares. ASK falling with load factor also falling means
   demand fell faster than supply, which is not.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("apix.capacity")

DEFAULT_PATH = Path("data/dgca_carrier_stats.csv")

# Carriers that fly cargo only. They report departures and zero passengers, so
# including them would drag the industry load factor toward zero.
CARGO_ONLY = {"BZ", "QO"}


def load(path: str | Path = DEFAULT_PATH,
         service: str = "scheduled_domestic") -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        log.warning(
            "%s not found. Run: python -m scripts.ingest_carrier_stats "
            "data/carriers/*.xlsx", p)
        return pd.DataFrame()
    df = pd.read_csv(p, parse_dates=["period"])
    df = df[df["service_type"] == service]
    return df[~df["carrier"].isin(CARGO_ONLY)].copy()


def industry_monthly(df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Roll carriers up to a national monthly supply picture."""
    df = load() if df is None else df
    if df.empty:
        return df

    agg = (df.groupby("period")
           .agg(departures=("departures", "sum"),
                passengers=("passengers", "sum"),
                rpk_thousand=("rpk_thousand", "sum"),
                ask_thousand=("ask_thousand", "sum"),
                carriers=("carrier", "nunique"))
           .reset_index()
           .sort_values("period"))

    # Industry load factor must be recomputed from summed RPK and ASK. Taking
    # the mean of published carrier load factors would weight a regional
    # operator the same as IndiGo.
    agg["load_factor"] = (100 * agg["rpk_thousand"] / agg["ask_thousand"]).round(2)
    agg["ask_mom_pct"] = (agg["ask_thousand"].pct_change(fill_method=None) * 100).round(2)
    agg["ask_yoy_pct"] = (agg["ask_thousand"].pct_change(12, fill_method=None) * 100).round(2)
    agg["pax_mom_pct"] = (agg["passengers"].pct_change(fill_method=None) * 100).round(2)
    agg["load_factor_change_pp"] = agg["load_factor"].diff().round(2)
    return agg


def carrier_shares(df: Optional[pd.DataFrame] = None,
                   period: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """Capacity share by carrier — who actually sets the price on a route."""
    df = load() if df is None else df
    if df.empty:
        return df
    period = period or df["period"].max()
    m = df[df["period"] == period].copy()
    total = m["ask_thousand"].sum()
    m["ask_share_pct"] = (100 * m["ask_thousand"] / total).round(2)
    m["pax_share_pct"] = (100 * m["passengers"] / m["passengers"].sum()).round(2)
    return (m[["period", "carrier", "carrier_name", "departures", "passengers",
               "ask_thousand", "pax_load_factor", "ask_share_pct", "pax_share_pct"]]
            .sort_values("ask_share_pct", ascending=False)
            .reset_index(drop=True))


def hhi(df: Optional[pd.DataFrame] = None,
        period: Optional[pd.Timestamp] = None) -> dict:
    """Herfindahl-Hirschman Index on domestic seat capacity.

    Worth reporting because it frames what APIx is measuring. Indian domestic
    aviation is highly concentrated, and in a concentrated market a fare index
    partly tracks one carrier's pricing decisions rather than a competitive
    market clearing. Say so rather than letting a reader assume otherwise.
    """
    shares = carrier_shares(df, period)
    if shares.empty:
        return {}
    s = shares["ask_share_pct"]
    index = float((s ** 2).sum())
    return {
        "period": str(shares["period"].iloc[0].date()),
        "hhi": round(index, 1),
        "interpretation": (
            "highly concentrated" if index > 2500
            else "moderately concentrated" if index > 1500
            else "competitive"
        ),
        "largest_carrier": shares["carrier"].iloc[0],
        "largest_share_pct": float(s.iloc[0]),
        "n_carriers": int(len(shares)),
        "note": (
            "US DOJ thresholds: above 2500 is highly concentrated. In a market "
            "this concentrated the index partly reflects one carrier's pricing "
            "policy rather than competitive clearing."
        ),
    }


def explain_move(apix_monthly: pd.DataFrame,
                 capacity: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Label each month's fare move as demand-led, capacity-led, or neither.

    The labels are a reading aid, not a causal claim. Two monthly series moving
    together is not identification, and anyone who has done an econometrics
    course will say so — which is why the output keeps the underlying numbers
    beside the label so a reader can disagree.
    """
    cap = industry_monthly() if capacity is None else capacity
    if cap.empty or apix_monthly.empty:
        return pd.DataFrame()

    a = apix_monthly.copy()
    a["period"] = pd.to_datetime(a["quote_date"] if "quote_date" in a else a["period"])
    a["period"] = a["period"].dt.to_period("M").dt.to_timestamp()

    m = a.merge(cap, on="period", how="inner")
    if m.empty:
        log.warning("no overlapping months between APIx and DGCA capacity — "
                    "capacity data runs %s to %s",
                    cap["period"].min().date(), cap["period"].max().date())
        return m

    fare_up = m["change_pct"] > 0.5
    ask_down = m["ask_mom_pct"] < -0.5
    lf_up = m["load_factor_change_pp"] > 0.5

    m["reading"] = np.select(
        [
            fare_up & ask_down & lf_up,
            fare_up & ask_down & ~lf_up,
            fare_up & ~ask_down & lf_up,
            fare_up & ~ask_down & ~lf_up,
            ~fare_up & ask_down,
        ],
        [
            "capacity withdrawn into firm demand",
            "capacity withdrawn, demand also softer",
            "demand outpacing steady capacity",
            "fares up without a clear supply or demand signal",
            "fares steady or easing despite tighter capacity",
        ],
        default="no clear signal",
    )
    return m[["period", "apix", "change_pct", "ask_thousand", "ask_mom_pct",
              "load_factor", "load_factor_change_pp", "reading"]]