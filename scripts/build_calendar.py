#!/usr/bin/env python3
"""Build the demand-event calendar that explains airfare seasonality.

    python -m scripts.build_calendar --from 2026-01-01 --to 2027-03-31

Without this, your month-on-month index looks erratic and you cannot answer
the first question a judge asks: "why did fares spike in that week?"

Two things matter for airfares specifically, and neither is just "is today a
public holiday":

  * The **departure** date drives the fare, not the collection date. A fare
    collected on 25 Oct for a 8 Nov departure is a Diwali fare. So every event
    flag is attached to the departure date and the index joins on that.
  * Travel demand clusters *around* an event, not on it — outbound in the days
    before, return in the days after — and the shoulder is asymmetric. A one-
    day dummy will miss most of the effect.

Festival dates in India are lunar and move 10-20 days a year, so they are
computed with the `holidays` package rather than hardcoded. Hardcoding them is
the single most common way these calendars silently rot.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

try:
    import holidays
except ImportError:
    sys.exit("pip install holidays")

# Events that move air traffic, with an asymmetric window around the date.
# (days_before, days_after). Diwali is the biggest domestic travel event of the
# year and the return leg runs well past the date itself.
TRAVEL_WINDOWS = {
    "Diwali (Deepavali)": (7, 6),
    "Dussehra": (5, 4),
    "Holi": (4, 3),
    "Christmas": (7, 10),          # runs into New Year
    "Eid al-Fitr": (4, 4),
    "Eid al-Adha": (3, 3),
    "Guru Nanak's Birthday": (2, 2),
    "Janmashtami (Vaishnava)": (2, 2),
    "Ram Navami": (2, 2),
    "Independence Day": (3, 2),
    "Republic Day": (3, 2),
    "Mahatma Gandhi's Birthday": (3, 2),
    "Good Friday": (3, 3),
    "Buddha Purnima": (2, 2),
}
DEFAULT_WINDOW = (2, 2)

# Fixed-window seasons that are not single-day holidays.
SEASONS = [
    ("summer_school_holidays", (5, 1), (6, 30),
     "peak leisure; capacity tight, fares high"),
    ("winter_peak", (12, 18), (1, 5),
     "Christmas-New Year; the highest fares of the year"),
    ("monsoon_lean", (7, 1), (8, 31),
     "lean season; discounting, but weather cancellations distort quotes"),
    ("wedding_season_1", (11, 15), (12, 15), "north India wedding demand"),
    ("wedding_season_2", (1, 15), (3, 10), "second wedding window"),
]


def build(start: date, end: date) -> pd.DataFrame:
    years = sorted({start.year, end.year, end.year + 1})
    hol = holidays.India(years=years, language="en_US")

    idx = pd.date_range(start, end, freq="D")
    df = pd.DataFrame({"departure_date": idx.date})
    df["dow"] = idx.dayofweek                       # 0 = Monday
    df["is_weekend"] = df["dow"].isin([5, 6])
    # Friday and Sunday carry the leisure peak in India, not Saturday.
    df["is_peak_dow"] = df["dow"].isin([4, 6])
    df["month"] = idx.month
    df["day_of_year"] = idx.dayofyear

    df["is_holiday"] = [d in hol for d in df["departure_date"]]
    df["holiday_name"] = [hol.get(d) for d in df["departure_date"]]

    df["event_window"] = None
    df["days_to_event"] = pd.NA
    for hdate, name in sorted(hol.items()):
        before, after = TRAVEL_WINDOWS.get(name, DEFAULT_WINDOW)
        lo, hi = hdate - timedelta(days=before), hdate + timedelta(days=after)
        m = (df["departure_date"] >= lo) & (df["departure_date"] <= hi)
        if not m.any():
            continue
        # A later event overwrites an earlier one where windows overlap: the
        # nearer event dominates demand.
        df.loc[m, "event_window"] = name
        df.loc[m, "days_to_event"] = [(d - hdate).days for d in df.loc[m, "departure_date"]]

    for label, (m1, d1), (m2, d2), _note in SEASONS:
        col = f"season_{label}"
        if (m1, d1) <= (m2, d2):
            m = df.apply(lambda r: (r["month"], r["departure_date"].day) >= (m1, d1)
                         and (r["month"], r["departure_date"].day) <= (m2, d2), axis=1)
        else:   # wraps the year boundary, e.g. 18 Dec -> 5 Jan
            m = df.apply(lambda r: (r["month"], r["departure_date"].day) >= (m1, d1)
                         or (r["month"], r["departure_date"].day) <= (m2, d2), axis=1)
        df[col] = m

    # A single flag for the regressions: is this departure inside any demand
    # surge? Convenient, but keep the individual columns — "fares rose because
    # of Diwali" and "fares rose because it was a long weekend" are different
    # claims and a judge will ask which one it was.
    season_cols = [c for c in df.columns if c.startswith("season_")]
    df["is_surge"] = (
        df["event_window"].notna()
        | df[season_cols].any(axis=1)
        | (df["is_holiday"] & df["is_weekend"])
    )

    # Long weekends: a holiday adjacent to a weekend, which in India reliably
    # produces a 3-4 day domestic travel spike.
    hol_or_wknd = df["is_holiday"] | df["is_weekend"]
    run = (~hol_or_wknd).cumsum()
    lengths = hol_or_wknd.groupby(run).transform("sum")
    df["long_weekend"] = hol_or_wknd & (lengths >= 3)

    return df.drop(columns=["dow"])


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="start", type=date.fromisoformat,
                   default=date.today())
    p.add_argument("--to", dest="end", type=date.fromisoformat,
                   default=date.today() + timedelta(days=400))
    p.add_argument("--out", default="data/event_calendar.csv")
    args = p.parse_args()

    df = build(args.start, args.end)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.out, index=False)

    print(f"{len(df)} days, {args.start} to {args.end} -> {args.out}")
    surge = df["is_surge"].mean()
    print(f"surge days: {df['is_surge'].sum()} ({100*surge:.0f}%)")
    print(f"long-weekend days: {df['long_weekend'].sum()}")
    if surge > 0.40:
        print(
            f"\n  WARNING: is_surge fires on {100*surge:.0f}% of days. A dummy that "
            "is true most of the time has no\n  discriminating power — do not use it "
            "as a regressor. The broad SEASONS windows are\n  the cause. Either narrow "
            "them, or use the individual season_* and event_window\n  columns, which "
            "separate 'Diwali' from 'it was a long weekend'."
        )
    ev = df[df["event_window"].notna()].groupby("event_window").size()
    print(f"\nevent windows ({len(ev)}):")
    print(ev.sort_values(ascending=False).head(12).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
