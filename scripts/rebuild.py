#!/usr/bin/env python3
"""Index rebuild. Runs after collection, and on demand after a methodology change.

    python -m scripts.rebuild                 # from stored cells (fast)
    python -m scripts.rebuild --reprocess     # from raw quotes (after a
                                              # normalize.py change)

The whole series is recomputed every time rather than appending a row. An
index is a function of its entire history and its weights — if you only ever
append, a fix to a cell from three weeks ago never propagates, and nobody
notices until someone asks why two exports disagree.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from apix.collector import index_config_from_basket, load_basket
from apix.index import build_index, impute_cells
from apix.normalize import run_pipeline
from apix.store import Store

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
log = logging.getLogger("apix.rebuild")


def rebuild_from_cells(store: Store, cfg) -> dict:
    cells = store.load_cells(cabin=cfg.cabin)
    if cells.empty:
        raise SystemExit("no elementary cells stored yet")
    cells["quote_date"] = pd.to_datetime(cells["quote_date"]).dt.date
    log.info("loaded %d cells over %d days", len(cells), cells["quote_date"].nunique())

    # Reconstruct the frame build_index expects. Going in at the cell level
    # skips re-deriving the Jevons means, which is the point.
    cells = impute_cells(cells[["quote_date", "route", "apw", "price"]]
                         .assign(n_quotes=99, n_carriers=99), cfg)
    return _aggregate_cells(cells, cfg)


def _aggregate_cells(cells: pd.DataFrame, cfg) -> dict:
    """Levels 2 and 3 only. Mirrors build_index's tail so the two paths cannot
    silently diverge — if you change weighting, change it in index.py and both
    paths follow."""
    import numpy as np
    from apix.index import BASE_VALUE, _drop_unfillable, _resample

    cfg = cfg.normalised()
    cells = _drop_unfillable(cells, "rebuild")
    if cells.empty:
        raise SystemExit("no cells survived imputation — nothing to aggregate")
    cells = cells[cells["route"].isin(cfg.route_weights)]
    cells = cells[cells["apw"].isin(cfg.apw_weights)]
    cells["month"] = pd.to_datetime(cells["quote_date"]).dt.strftime("%Y-%m")
    base_month = cfg.base_period or cells["month"].min()
    base = (cells[cells["month"] == base_month]
            .groupby(["route", "apw"])["price"].mean().rename("base_price"))
    if base.empty:
        raise SystemExit(f"base period {base_month} has no observations")

    cells = cells.merge(base.reset_index(), on=["route", "apw"], how="inner")
    cells["relative"] = cells["price"] / cells["base_price"]
    cells["w_apw"] = cells["apw"].map(cfg.apw_weights)
    cells["w_route"] = cells["route"].map(cfg.route_weights)

    route_daily = (cells.groupby(["quote_date", "route"])
                   .apply(lambda g: pd.Series({
                       "index": BASE_VALUE * np.average(g["relative"], weights=g["w_apw"]),
                       "n_cells": len(g),
                       "pct_imputed": 100 * g["imputed"].mean()}),
                       include_groups=False).reset_index())

    headline = (cells.groupby("quote_date")
                .apply(lambda g: pd.Series({
                    "apix": BASE_VALUE * np.average(g["relative"],
                                                    weights=g["w_apw"] * g["w_route"]),
                    "n_quotes_cells": len(g),
                    "pct_imputed": 100 * g["imputed"].mean()}),
                    include_groups=False).reset_index().sort_values("quote_date"))
    headline["dod_pct"] = headline["apix"].pct_change(fill_method=None) * 100
    headline["mom_pct"] = headline["apix"].pct_change(30, fill_method=None) * 100
    headline["yoy_pct"] = headline["apix"].pct_change(365, fill_method=None) * 100

    return {"cells": cells, "route_daily": route_daily, "daily": headline,
            "weekly": _resample(headline, "W-SUN"),
            "monthly": _resample(headline, "MS")}


def rebuild_from_quotes(store: Store, cfg, days: int) -> dict:
    end = date.today()
    start = end - timedelta(days=days)
    log.info("full reprocess from raw quotes, %s to %s", start, end)
    raw = store.load_quotes(start, end)
    if raw.empty:
        raise SystemExit("no raw quotes in that window")
    df = run_pipeline_from_frame(raw)
    store.write_quotes(df)          # drop_reason may have changed
    return build_index(df, cfg)


def run_pipeline_from_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """Re-run cleaning over rows already in the canonical column layout."""
    from apix.normalize import bucket_apw, clean, decompose_fare
    df = raw.copy()
    df["quote_ts"] = pd.to_datetime(df["quote_ts"], utc=True)
    df["quote_date"] = df["quote_ts"].dt.date
    df["departure_date"] = pd.to_datetime(df["departure_date"]).dt.date
    df["apw_bucket"] = bucket_apw(df["apw_days"])
    df["drop_reason"] = None
    return decompose_fare(clean(df))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--basket", default="config/basket.yaml")
    p.add_argument("--reprocess", action="store_true",
                   help="rebuild from raw quotes instead of stored cells")
    p.add_argument("--days", type=int, default=400)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--out", default="out")
    args = p.parse_args()

    basket = load_basket(args.basket)
    cfg = index_config_from_basket(basket)
    store = Store(dry_run=args.dry_run)
    basket_id = store.register_basket(basket)

    result = (rebuild_from_quotes(store, cfg, args.days) if args.reprocess
              else rebuild_from_cells(store, cfg))

    counts = store.write_index(result, basket_id=basket_id)
    log.info("published: %s", counts)

    # Also drop parquet so the Space can warm its cache without a round trip.
    Path(args.out).mkdir(parents=True, exist_ok=True)
    for name, frame in result.items():
        frame.to_parquet(f"{args.out}/apix_{name}.parquet", index=False)

    d = result["daily"]
    log.info("APIx latest: %.2f on %s (%.2f%% d/d, %.1f%% imputed)",
             d["apix"].iloc[-1], d["quote_date"].iloc[-1],
             d["dod_pct"].iloc[-1], d["pct_imputed"].iloc[-1])

    if d["pct_imputed"].iloc[-1] > 25:
        log.warning("latest day is over 25%% imputed — check source health "
                    "before treating this number as publishable")
    return 0


if __name__ == "__main__":
    sys.exit(main())