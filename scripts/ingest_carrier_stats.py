#!/usr/bin/env python3
"""Parse DGCA Monthly Traffic and Operating Statistics workbooks.

    python -m scripts.ingest_carrier_stats data/carriers/*.xlsx

One workbook per carrier per year, each holding four stacked blocks:
Scheduled Domestic, Scheduled International, Non-Scheduled Domestic,
Non-Scheduled International. Only Scheduled Domestic matters for APIx, but all
four are parsed so the file is not re-read when someone asks a different
question.

Why this data earns its place in a price index project: it carries **ASK**
(available seat kilometres) and **RPK** (revenue passenger kilometres) monthly
by carrier. Without capacity you cannot distinguish the two explanations for a
fare rise — demand went up, or seats were withdrawn — and that distinction is
the first thing an economist will ask about any spike APIx reports.

Layout, verified against the 2026 files:

    row+0   2026 | ... (Scheduled Domestic Services)     <- year and block type
    row+1   MONTH | AIRCRAFT FLOWN | ... (merged header)
    row+2   | DEPARTURES | HOURS | KILOMETRE | ...       (second header row)
    row+3   JAN | 58905 | 52623.353 | 9704113 | ...
    ...     DEC
    row+15  TOTAL                                        <- skip, derivable
    row+16  SOURCE:- ICAO ATR FORM A FURNISHED BY ...
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

YEAR = re.compile(r"^(19|20)\d{2}(\.0)?$")
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUNE": 6,
    "JUL": 7, "JULY": 7, "AUG": 8, "SEP": 9, "SEPT": 9, "OCT": 10,
    "NOV": 11, "DEC": 12,
}

# Column positions are fixed by the DGCA template. Named here rather than
# inferred from headers, because the headers are merged cells that pandas
# renders as NaN in unpredictable places.
COLS = {
    1: "departures",
    3: "hours_flown",
    4: "passengers",          # NOTE: the template shifts — resolved below
    5: "rpk_thousand",
    6: "ask_thousand",
    7: "pax_load_factor",
    8: "freight_tonne",
    9: "mail_tonne",
    10: "cargo_total_tonne",
    11: "tkm_passenger_thousand",
    12: "tkm_freight_thousand",
    13: "tkm_mail_thousand",
    15: "atk_thousand",
    16: "weight_load_factor",
}

CARRIER_TO_IATA = {
    "INDIGO": "6E", "AIR INDIA": "AI", "AIR INDIA EXPRESS": "IX",
    "SPICEJET": "SG", "AKASA AIR": "QP", "ALLIANCE AIR": "9I",
    "FLY91": "IC", "INDIA ONE AIR": "I1", "BLUEDART": "BZ",
    "BLUE DART": "BZ", "QUIKJET": "QO", "QUIKJET CARGO": "QO",
    "STAR AIR": "S5", "TRUJET": "2T",
}

BLOCK_TYPES = [
    ("scheduled_domestic", ("SCHEDULED DOMESTIC",), ("NON-",)),
    ("scheduled_international", ("SCHEDULED INTERNATIONAL",), ("NON-",)),
    ("nonscheduled_domestic", ("NON- SCHEDULED DOMESTIC", "NON-SCHEDULED DOMESTIC"), ()),
    ("nonscheduled_international",
     ("NON- SCHEDULED INTERNAT", "NON-SCHEDULED INTERNAT"), ()),
]


def classify(title: str) -> str:
    t = re.sub(r"\s+", " ", str(title)).upper()
    for name, needles, forbidden in BLOCK_TYPES:
        if any(n in t for n in needles) and not any(f in t for f in forbidden):
            return name
    return "unknown"


def carrier_from(df: pd.DataFrame, row: int, path: Path) -> str:
    """Carrier name sits in a merged cell on the block's title row."""
    for col in (14, 13, 15, 12):
        v = df.iat[row, col] if col < df.shape[1] else None
        if isinstance(v, str) and v.strip() and not v.strip().isdigit():
            return re.sub(r"\s+", " ", v.strip()).upper()
    # Fall back to the filename: indigo26.xlsx -> INDIGO
    return re.sub(r"[_\-]?\d+$", "", path.stem).replace("_", " ").upper()


def parse_book(path: Path) -> pd.DataFrame:
    xl = pd.ExcelFile(path)
    frames = []
    for sheet in xl.sheet_names:
        df = xl.parse(sheet, header=None)
        starts = [i for i, v in enumerate(df[0]) if YEAR.fullmatch(str(v).strip())]
        for row in starts:
            year = int(float(str(df.iat[row, 0]).strip()))
            block = classify(df.iat[row, 2])
            carrier = carrier_from(df, row, path)

            for r in range(row + 1, min(row + 18, len(df))):
                label = str(df.iat[r, 0]).strip().upper()
                if label in ("TOTAL", "NAN", ""):
                    continue
                month = MONTHS.get(label)
                if month is None:
                    continue
                rec = {
                    "year": year,
                    "month": month,
                    "period": pd.Timestamp(year, month, 1),
                    "carrier_name": carrier,
                    "carrier": CARRIER_TO_IATA.get(carrier, "??"),
                    "service_type": block,
                    "source_file": path.name,
                }
                for col, field in COLS.items():
                    v = df.iat[r, col] if col < df.shape[1] else None
                    rec[field] = pd.to_numeric(v, errors="coerce")
                # A month with no departures is an unreported month, not a
                # month with zero flying. Keeping it as zero would drag every
                # capacity average down.
                if pd.isna(rec["departures"]) and pd.isna(rec.get("passengers")):
                    continue
                frames.append(rec)
    return pd.DataFrame(frames)


def derive(df: pd.DataFrame) -> pd.DataFrame:
    """Add the fields the index work actually uses."""
    if df.empty:
        return df
    out = df.sort_values(["carrier", "service_type", "period"]).copy()

    # Load factor is published, but recompute it as a parse check: if the
    # published and derived values disagree, a column shifted.
    # Cargo-only carriers report zero ASK, so guard the division rather than
    # letting it produce inf and poison the comparison below.
    import numpy as np
    ask = pd.to_numeric(out["ask_thousand"], errors="coerce").replace(0, np.nan)
    rpk = pd.to_numeric(out["rpk_thousand"], errors="coerce")
    out["load_factor_derived"] = (100 * rpk / ask).round(2)
    out["load_factor_gap"] = (
        out["pax_load_factor"] - out["load_factor_derived"]).abs()

    grp = out.groupby(["carrier", "service_type"])
    out["ask_yoy_pct"] = grp["ask_thousand"].pct_change(12, fill_method=None) * 100
    out["ask_mom_pct"] = grp["ask_thousand"].pct_change(fill_method=None) * 100
    out["pax_yoy_pct"] = grp["passengers"].pct_change(12, fill_method=None) * 100
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("files", nargs="+")
    p.add_argument("--out", default="data/dgca_carrier_stats.csv")
    a = p.parse_args()

    frames = []
    for f in a.files:
        path = Path(f)
        try:
            b = parse_book(path)
        except Exception as exc:  # noqa: BLE001
            print(f"  {path.name}: failed — {exc}")
            continue
        if b.empty:
            print(f"  {path.name}: no month rows found")
            continue
        print(f"  {path.name}: {len(b)} rows, "
              f"{b['carrier_name'].iloc[0]} ({b['carrier'].iloc[0]})")
        frames.append(b)

    if not frames:
        sys.exit("nothing parsed")

    df = derive(pd.concat(frames, ignore_index=True))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        old = pd.read_csv(out, parse_dates=["period"])
        df = pd.concat([old, df], ignore_index=True).drop_duplicates(
            subset=["period", "carrier", "service_type"], keep="last")
    df = df.sort_values(["period", "carrier", "service_type"]).reset_index(drop=True)
    df.to_csv(out, index=False)

    dom = df[df["service_type"] == "scheduled_domestic"]
    print(f"\n{len(df)} rows -> {out}")
    print(f"scheduled domestic: {dom['carrier'].nunique()} carriers, "
          f"{dom['period'].min():%b %Y} to {dom['period'].max():%b %Y}")

    bad = dom[dom["load_factor_gap"] > 0.5]
    if not bad.empty:
        print(f"\n  WARNING: {len(bad)} rows where published load factor and "
              "RPK/ASK disagree by >0.5pp.\n  A column has probably shifted in "
              "those workbooks — check before using capacity figures.")

    unknown = sorted(set(df.loc[df["carrier"] == "??", "carrier_name"]))
    if unknown:
        print(f"\n  unmapped carriers (add to CARRIER_TO_IATA): {unknown}")

    latest = dom[dom["period"] == dom["period"].max()]
    print(f"\ndomestic capacity, {dom['period'].max():%b %Y}:")
    print(latest[["carrier", "departures", "passengers", "ask_thousand",
                  "pax_load_factor"]]
          .sort_values("ask_thousand", ascending=False).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())