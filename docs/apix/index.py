"""APIx index construction.

Structure mirrors how NSO actually builds a CPI sub-index, because the whole
point is that this could slot into the Transport group:

  Level 0  quote          one priced itinerary
  Level 1  elementary     (route x APW x cabin) on a day
                          -> Jevons geometric mean, unweighted
  Level 2  route          weighted mean of APW cells (booking-lead-time profile)
  Level 3  headline       weighted mean of routes (DGCA passenger-share weights)

Why Jevons at the elementary level: it is what NSO/ILO recommend for products
with high price dispersion and substitution, and it is what happens when a
traveller shops around. Carli (arithmetic mean of relatives) has a known upward
bias that would be severe on airfares, where within-cell dispersion is 200-400%.

Aggregation above the elementary level is a modified Laspeyres: weights are
fixed at the base period and the index is a weighted mean of price relatives,
so it is consistent with how the CPI Transport sub-group is compiled and can be
chain-linked when the basket is refreshed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Dict, Optional

import numpy as np
import pandas as pd

log = logging.getLogger("apix.index")

BASE_VALUE = 100.0


@dataclass
class IndexConfig:
    route_weights: Dict[str, float]          # "DEL-BOM" -> share, sums to 1
    apw_weights: Dict[int, float]            # 1/7/15/30/45 -> share, sums to 1
    cabin: str = "ECONOMY"
    base_period: Optional[str] = None        # "2026-04" ; default = first month
    min_quotes_per_cell: int = 3
    max_imputation_days: int = 7             # carry a cell forward at most this long

    def normalised(self) -> "IndexConfig":
        rw = _renorm(self.route_weights)
        aw = _renorm({int(k): v for k, v in self.apw_weights.items()})
        return IndexConfig(rw, aw, self.cabin, self.base_period,
                           self.min_quotes_per_cell, self.max_imputation_days)


def _renorm(d: Dict) -> Dict:
    total = float(sum(d.values()))
    if total <= 0:
        raise ValueError("weights must sum to a positive number")
    return {k: v / total for k, v in d.items()}


# --------------------------------------------------------------- level 1
def elementary_aggregates(clean_df: pd.DataFrame, cfg: IndexConfig) -> pd.DataFrame:
    """Jevons geometric mean per (date, route, apw, cabin) cell.

    Uses the *cheapest fare per carrier* within the cell rather than every
    quote, so a carrier that publishes 14 fare buckets on a route does not
    dominate a carrier that publishes 3. This is the airline analogue of "one
    price per outlet" in manual CPI collection.
    """
    df = clean_df[clean_df["drop_reason"].isna()].copy()
    df = df[df["cabin"] == cfg.cabin]
    df = df[df["apw_bucket"].notna()]
    if df.empty:
        return pd.DataFrame(columns=["quote_date", "route", "apw", "price", "n_quotes", "n_carriers"])

    df["route"] = df["origin"] + "-" + df["destination"]
    df["apw"] = df["apw_bucket"].astype(int)

    per_carrier = (
        df.groupby(["quote_date", "route", "apw", "carrier"], as_index=False)["total_fare"]
        .min()
    )

    cell = (
        per_carrier.groupby(["quote_date", "route", "apw"])
        .agg(
            price=("total_fare", lambda s: float(np.exp(np.log(s).mean()))),
            n_carriers=("carrier", "nunique"),
        )
        .reset_index()
    )
    counts = df.groupby(["quote_date", "route", "apw"]).size().rename("n_quotes").reset_index()
    cell = cell.merge(counts, on=["quote_date", "route", "apw"], how="left")
    cell.loc[cell["n_quotes"] < cfg.min_quotes_per_cell, "price"] = np.nan

    # A one-carrier cell is not a Jevons mean of anything — it is a single
    # observation wearing the same name. That is acceptable with a
    # cheapest-fare-only source, but it must be visible rather than implied,
    # because it means the index carries no within-cell dispersion.
    thin = cell["price"].notna() & (cell["n_carriers"] <= 1)
    if thin.any():
        log.info(
            "%d/%d cells rest on a single carrier quote (%.0f%%). The "
            "elementary aggregate is that one price, so within-cell dispersion "
            "is zero — state this in the methodology note.",
            int(thin.sum()), len(cell), 100 * thin.sum() / max(len(cell), 1),
        )
    return cell.sort_values(["quote_date", "route", "apw"]).reset_index(drop=True)


# --------------------------------------------------------------- imputation
def impute_cells(cell: pd.DataFrame, cfg: IndexConfig) -> pd.DataFrame:
    """Fill gaps the way CPI does: carry forward briefly, then class-impute.

    A missing cell is *not* zero and *not* a price fall. Carrying forward for a
    few days is standard; beyond that the cell's own history is stale, so we
    move it by the average movement of the other cells on the same route
    (class imputation), which is the ILO-recommended treatment.
    """
    if cell.empty:
        return cell
    out = cell.copy()
    out["imputed"] = out["price"].isna()

    full_idx = pd.MultiIndex.from_product(
        [sorted(out["quote_date"].unique()), sorted(out["route"].unique()), sorted(out["apw"].unique())],
        names=["quote_date", "route", "apw"],
    )
    out = out.set_index(["quote_date", "route", "apw"]).reindex(full_idx).reset_index()
    # Reindexing introduces NaN for cells that were never observed. Cast
    # explicitly rather than relying on fillna to downcast an object column —
    # pandas is removing that behaviour, and the resulting object dtype would
    # silently break the .mean() that produces pct_imputed.
    # Convert dtype before filling. `.fillna(...).astype(bool)` still runs the
    # fill against an object column, which is the deprecated implicit downcast
    # — the astype at the end does not prevent the warning or the future
    # behaviour change.
    out["imputed"] = out["imputed"].astype("boolean").fillna(True).astype(bool)

    # Route-level daily movement, from cells that are actually observed.
    obs = out.dropna(subset=["price"]).copy()
    obs["logp"] = np.log(obs["price"])
    route_mean = obs.groupby(["route", "quote_date"])["logp"].mean().rename("route_logmean")
    out = out.merge(route_mean.reset_index(), on=["route", "quote_date"], how="left")

    filled = []
    for (route, apw), grp in out.sort_values("quote_date").groupby(["route", "apw"]):
        g = grp.copy()
        ffill = g["price"].ffill()
        age = g["price"].notna().cumsum()
        since = g.groupby(age).cumcount()
        carried = ffill.where(since <= cfg.max_imputation_days)

        # Class imputation for the rest: last observed price scaled by the
        # route's movement between then and now.
        anchor_log = np.log(g["price"]).ffill()
        anchor_route = g["route_logmean"].where(g["price"].notna()).ffill()
        class_imp = np.exp(anchor_log + (g["route_logmean"] - anchor_route))

        g["price"] = g["price"].fillna(carried).fillna(class_imp)
        filled.append(g)

    out = pd.concat(filled, ignore_index=True).drop(columns=["route_logmean"])
    return out.sort_values(["quote_date", "route", "apw"]).reset_index(drop=True)



def _drop_unfillable(cells, where: str):
    """Remove cells imputation could not fill, and say how many.

    On the first day of collection there is no history to carry forward and no
    anchor for class imputation, so every unobserved cell in the reindexed grid
    stays NaN. Passing those into a weighted average yields NaN, and a null
    index value is worse than an absent one: it occupies a date, implies the
    day was computed, and violates the not-null constraint on apix_daily.

    An index value that cannot be computed should be missing, not null.
    """
    bad = cells["price"].isna()
    if bad.any():
        log.warning(
            "%s: %d/%d cells could not be imputed and are excluded. On the "
            "first days of collection this is expected — imputation needs "
            "history. Watch that it falls as the series grows.",
            where, int(bad.sum()), len(cells),
        )
    return cells[~bad].copy()


# --------------------------------------------------------------- levels 2 & 3
def build_index(clean_df: pd.DataFrame, cfg: IndexConfig) -> Dict[str, pd.DataFrame]:
    cfg = cfg.normalised()
    cell = elementary_aggregates(clean_df, cfg)
    if cell.empty:
        raise ValueError("no elementary aggregates could be formed from this data")
    cell = impute_cells(cell, cfg)
    cell = _drop_unfillable(cell, "build_index")
    if cell.empty:
        raise ValueError(
            "no cells survived imputation — nothing observed in the basket"
        )

    cell = cell[cell["route"].isin(cfg.route_weights)]
    cell = cell[cell["apw"].isin(cfg.apw_weights)]
    if cell.empty:
        raise ValueError("basket routes/APWs not present in the data")

    # Base prices: the mean cell price over the base period.
    cell["month"] = pd.to_datetime(cell["quote_date"]).dt.strftime("%Y-%m")
    base_month = cfg.base_period or cell["month"].min()
    base = (
        cell[cell["month"] == base_month]
        .groupby(["route", "apw"])["price"].mean().rename("base_price")
    )
    if base.empty:
        available = sorted(cell["month"].unique())
        raise ValueError(
            f"base period {base_month} has no observations. Months present: "
            f"{', '.join(available) if available else 'none'}. "
            f"Set base_period in config/basket.yaml to the month collection "
            f"actually started, or leave it null to use the earliest month "
            f"available. The base period is what every index value is measured "
            f"against, so it must not be guessed."
        )

    cell = cell.merge(base.reset_index(), on=["route", "apw"], how="inner")
    cell["relative"] = cell["price"] / cell["base_price"]

    cell["w_apw"] = cell["apw"].map(cfg.apw_weights)
    cell["w_route"] = cell["route"].map(cfg.route_weights)

    # Level 2: route index = weighted mean of APW relatives.
    route_daily = (
        cell.groupby(["quote_date", "route"])
        .apply(lambda g: pd.Series({
            "index": BASE_VALUE * np.average(g["relative"], weights=g["w_apw"]),
            "n_cells": len(g),
            "pct_imputed": 100 * g["imputed"].mean(),
        }), include_groups=False)
        .reset_index()
    )

    # Level 3: headline = weighted mean of route relatives.
    headline = (
        cell.groupby("quote_date")
        .apply(lambda g: pd.Series({
            "apix": BASE_VALUE * np.average(g["relative"], weights=g["w_apw"] * g["w_route"]),
            "n_quotes_cells": len(g),
            "pct_imputed": 100 * g["imputed"].mean(),
        }), include_groups=False)
        .reset_index()
        .sort_values("quote_date")
    )
    headline["dod_pct"] = headline["apix"].pct_change(fill_method=None) * 100
    headline["mom_pct"] = headline["apix"].pct_change(30, fill_method=None) * 100
    headline["yoy_pct"] = headline["apix"].pct_change(365, fill_method=None) * 100

    weekly = _resample(headline, "W-SUN")
    monthly = _resample(headline, "MS")

    return {
        "cells": cell,
        "route_daily": route_daily,
        "daily": headline,
        "weekly": weekly,
        "monthly": monthly,
        "base_period": pd.DataFrame([{"base_period": base_month, "cabin": cfg.cabin}]),
    }


def _resample(daily: pd.DataFrame, freq: str) -> pd.DataFrame:
    d = daily.copy()
    d["quote_date"] = pd.to_datetime(d["quote_date"])
    out = (
        d.set_index("quote_date")
        .resample(freq)
        .agg(apix=("apix", "mean"), days=("apix", "size"))
        .dropna()
        .reset_index()
    )
    out["change_pct"] = out["apix"].pct_change(fill_method=None) * 100
    return out


# --------------------------------------------------------------- diagnostics
def lead_time_curve(clean_df: pd.DataFrame, cfg: IndexConfig) -> pd.DataFrame:
    """Median fare and implied elasticity by advance-purchase window.

    Elasticity here is d(log price)/d(log lead time) — the number that answers
    "how much does booking a week earlier save on this route".
    """
    cell = elementary_aggregates(clean_df, cfg)
    if cell.empty:
        return pd.DataFrame()
    curve = (
        cell.groupby(["route", "apw"])["price"].median().reset_index()
        .sort_values(["route", "apw"])
    )
    curve["log_p"] = np.log(curve["price"])
    curve["log_apw"] = np.log(curve["apw"])
    curve["elasticity"] = (
        curve.groupby("route")
        .apply(lambda g: pd.Series(
            np.gradient(g["log_p"].values, g["log_apw"].values), index=g.index),
            include_groups=False)
        .reset_index(level=0, drop=True)
    )
    return curve


def sector_heatmap(clean_df: pd.DataFrame, cfg: IndexConfig) -> pd.DataFrame:
    cell = elementary_aggregates(clean_df, cfg)
    if cell.empty:
        return pd.DataFrame()
    return cell.pivot_table(index="route", columns="apw", values="price", aggfunc="median")