#!/usr/bin/env python3
"""Daily collection job. Entry point for .github/workflows/collect.yml

    python -m scripts.collect --date 2026-09-06
    python -m scripts.collect --dry-run
    python -m scripts.collect --backfill 5     # fill gaps in the last 5 days

Exit codes matter here — the workflow branches on them:
    0  collected and stored
    1  hard failure (config, credentials, no sources)
    2  ran, but collected too little to be usable (partial outage)
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from apix.collector import (
    collect_day, index_config_from_basket, load_basket, validate,
)
from apix.index import elementary_aggregates
from apix.store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("apix.job")

# A run that produces less than this fraction of the expected cells is a
# partial outage, not a normal day. Better to fail loudly and re-run than to
# publish an index built on a quarter of the basket.
MIN_CELL_COVERAGE = 0.60


def run_one(basket: dict, store: Store, run_date: date) -> int:
    expected = len(basket["routes"]) * len(basket["collection"]["windows"])
    run_id = store.start_run(source="scheduled")
    try:
        df = collect_day(basket, run_date)
        if df.empty:
            store.finish_run(run_id, status="failed", errors=["no quotes returned"])
            log.error("%s: no quotes at all", run_date)
            return 2

        n_quotes = store.write_quotes(df)

        cfg = index_config_from_basket(basket)
        cells = elementary_aggregates(df, cfg)
        # Store the *observed* cells only. Imputation is a property of the
        # index build, not of the observation, and baking it into the stored
        # cell would make yesterday's gap invisible forever.
        cells = cells[cells["price"].notna()]
        store.write_cells(cells, cabin=cfg.cabin)

        coverage = len(cells) / max(expected, 1)
        log.info("%s: %d quotes, %d/%d cells (%.0f%% coverage)",
                 run_date, n_quotes, len(cells), expected, 100 * coverage)

        if coverage < MIN_CELL_COVERAGE:
            store.finish_run(run_id, status="partial", quotes_stored=n_quotes,
                             errors=[f"cell coverage {coverage:.0%}"])
            return 2

        store.finish_run(run_id, status="ok", quotes_stored=n_quotes)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.exception("collection failed for %s", run_date)
        store.finish_run(run_id, status="failed", errors=[str(exc)[:500]])
        raise


# Sources that can genuinely reproduce a past observation date. Everything
# else observes only "now", whatever date you ask it for.
REPLAYABLE = ("replay:",)


def can_backfill(basket: dict) -> bool:
    return any(s["name"].startswith(REPLAYABLE)
               for s in basket.get("sources", []) if s.get("enabled"))


def find_gaps(store: Store, days: int, today: date) -> list[date]:
    have = set(store.collected_dates())
    want = [today - timedelta(days=i) for i in range(1, days + 1)]
    return sorted(d for d in want if d not in have)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--basket", default="config/basket.yaml")
    p.add_argument("--date", type=date.fromisoformat, default=None)
    p.add_argument("--backfill", type=int, default=0,
                   help="also re-collect any missing days in the last N days")
    p.add_argument("--dry-run", action="store_true",
                   help="skip database writes; sources still make requests")
    p.add_argument("--validate", action="store_true",
                   help="check config only — no network, no credentials, "
                        "no writes. This is what CI runs.")
    args = p.parse_args()

    basket = load_basket(args.basket)

    if args.validate:
        validate(basket)
        return 0
    store = Store(dry_run=args.dry_run)
    store.register_basket(basket)

    run_date = args.date or date.today()
    targets = [run_date]

    if args.backfill:
        gaps = find_gaps(store, args.backfill, run_date)
        if gaps and not can_backfill(basket):
            # Running a live source against a past date does not recover that
            # day — the request still happens now. Worse, it silently corrupts
            # the panel: departure dates are computed from the target date
            # while the observation timestamp is today, so every lead time is
            # offset by the gap and falls outside the tolerance of any target
            # window. On 2026-09-05 that produced 66 quotes and zero usable
            # cells, while 2026-09-06 produced 70 of each. Same code, different
            # offset.
            log.warning(
                "gaps at %s cannot be filled: no replay source is enabled and "
                "live sources only ever observe the present. Skipping backfill. "
                "Record these as missing days in the methodology note rather "
                "than collecting quotes that will not bucket.",
                ", ".join(str(g) for g in gaps),
            )
        elif gaps:
            log.info("backfilling from replay archive: %s",
                     ", ".join(str(g) for g in gaps))
            targets = gaps + targets

    worst = 0
    for d in targets:
        worst = max(worst, run_one(basket, store, d))
    return worst


if __name__ == "__main__":
    sys.exit(main())
