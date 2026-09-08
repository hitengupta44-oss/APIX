"""Backfill must not run against live-only sources.

Asking a live source for a past date does not recover that day — the request
still happens now. It also corrupts the panel: departure dates are computed
from the target date while quote_ts is today, so every lead time is offset by
the size of the gap and falls outside the tolerance of every target window.

Observed in production: 2026-09-05 produced 66 quotes and 0 usable cells, while
2026-09-06 produced 70 of each. Identical code, three-day offset versus two.
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from apix.normalize import bucket_apw
from scripts.collect import can_backfill


def basket(*enabled: str) -> dict:
    names = ["gds:aggregator", "ota:travelpayouts", "replay:archive"]
    return {"sources": [{"name": n, "enabled": n in enabled} for n in names]}


def test_live_only_cannot_backfill():
    assert not can_backfill(basket("ota:travelpayouts"))
    assert not can_backfill(basket("gds:aggregator"))
    assert not can_backfill(basket("gds:aggregator", "ota:travelpayouts"))


def test_replay_can_backfill():
    assert can_backfill(basket("replay:archive"))
    assert can_backfill(basket("ota:travelpayouts", "replay:archive"))


def test_no_sources_cannot_backfill():
    assert not can_backfill(basket())
    assert not can_backfill({})


@pytest.mark.parametrize("offset,expected_usable", [(0, 5), (1, 5), (2, 5), (3, 0)])
def test_offset_between_target_and_observation_destroys_bucketing(offset, expected_usable):
    """The mechanism itself.

    Departures are set for target_date + window; the observation happens
    `offset` days later. At an offset of 3 every window misses the +/-2
    tolerance simultaneously and the whole day yields nothing — which is
    exactly what the 09-05 backfill did.
    """
    windows = [1, 7, 15, 30, 45]
    target = date(2026, 9, 5)
    observed = date(2026, 9, 5 + offset)

    apw = pd.Series([(target + timedelta(days=w) - observed).days
                     for w in windows])
    buckets = bucket_apw(apw, windows=tuple(windows), tol=2)
    assert int(buckets.notna().sum()) == expected_usable


def test_the_two_production_days_reproduce():
    """09-05 (3-day offset) yields nothing; 09-06 (2-day offset) yields all."""
    windows = [1, 7, 15, 30, 45]
    observed = date(2026, 9, 8)

    def usable(target: date) -> int:
        apw = pd.Series([(target + timedelta(days=w) - observed).days
                         for w in windows])
        return int(bucket_apw(apw, windows=tuple(windows), tol=2).notna().sum())

    assert usable(date(2026, 9, 5)) == 0
    assert usable(date(2026, 9, 6)) == 5
