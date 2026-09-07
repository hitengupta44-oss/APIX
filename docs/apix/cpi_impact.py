"""Translate an APIx movement into its CPI contribution.

This is the module that turns a route-level airfare index into a sentence a
central banker cares about:

    "Airfares rose 6.2% in November. That contributed roughly 2.1 basis points
     to headline CPI inflation."

Arithmetic is deliberately simple, because the honest version is simple. The
contribution of one item to a weighted index is its weight times its price
relative change:

    contribution_pp = item_weight_share * pct_change

where item_weight_share is the CPI item weight expressed as a fraction of 100.
An item with weight 0.35 (i.e. 0.35% of the basket) moving 6.2% contributes
0.35/100 * 6.2 = 0.0217 percentage points, or about 2.2 basis points.

Three caveats that belong in your report, not buried in a docstring:

1. APIx is not the CPI air-fare item index. It uses a different basket
   (30 directional routes), a different collection cadence (daily), and a
   different lead-time mix. The contribution below is what headline CPI *would*
   show if the official item index moved as APIx did. That is an estimate of
   an impact, not a measurement of one.

2. The official item index is monthly. Comparing a daily APIx move to a monthly
   CPI weight overstates the implied volatility. Use monthly APIx for anything
   published.

3. CPI 2024 weights come from HCES 2023-24 and are fixed until the next base
   revision, so the weight does not move with fuel prices or traffic.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

log = logging.getLogger("apix.cpi")

# Division-wise weights, CPI 2024 (base 2024=100), from the first release
# dated 12 February 2026. Source: MoSPI press release, division-wise weights
# table. These are published and stable; the *item* weight is not here because
# it has to be read off cpi.mospi.gov.in — see docs/data-sources.md.
CPI_2024_WEIGHTS = {
    "07_transport": {"rural": 8.644, "urban": 8.985, "combined": 8.796},
    "08_information_communication": {"rural": 3.647, "urban": 3.563, "combined": 3.609},
}

# Under CPI 2012, Transport & Communication combined carried 6.394. Useful for
# the "transport now matters more" slide.
CPI_2012_TRANSPORT_COMMUNICATION_COMBINED = 6.394

# Series linking factors, 2012 -> 2024. Valid at GENERAL INDEX LEVEL ONLY.
# There is no sanctioned way to link the transport sub-index across the break,
# so do not build a long transport back-series.
LINKING_FACTORS = {"rural": 0.5222, "urban": 0.5320, "combined": 0.5267}


@dataclass
class ItemWeight:
    """CPI item weight, as a percentage of the whole basket (not a fraction)."""

    item: str
    rural: float
    urban: float
    combined: float
    source: str
    series: str = "CPI 2024 (base 2024=100)"

    def share(self, sector: str = "combined") -> float:
        return getattr(self, sector) / 100.0


def load_item_weight(
    path: str | Path = "data/cpi_2024_weights.csv",
    item_match: str = "air fare",
) -> Optional[ItemWeight]:
    """Read the air-fare item weight, once you have fetched it.

    Expected columns: item, rural, urban, combined
    See docs/data-sources.md for where to get the file.
    """
    p = Path(path)
    if not p.exists():
        log.warning(
            "%s not found — the CPI-impact figure cannot be computed. "
            "Fetch the item weights from cpi.mospi.gov.in (Announcement tab); "
            "see docs/data-sources.md.", p,
        )
        return None

    df = pd.read_csv(p, comment="#", skip_blank_lines=True)
    df.columns = [c.strip().lower() for c in df.columns]
    missing = {"item", "rural", "urban", "combined"} - set(df.columns)
    if missing:
        log.warning("%s is missing columns %s — expected item,rural,urban,combined",
                    p, sorted(missing))
        return None
    if df.empty:
        log.warning(
            "%s has headers but no rows. This is the unfilled template — fetch the "
            "real weights from cpi.mospi.gov.in (Announcement tab) and save as "
            "data/cpi_2024_weights.csv.", p,
        )
        return None

    hit = df[df["item"].str.strip().str.lower().str.contains(item_match.lower(), na=False)]
    if hit.empty:
        log.warning("no item matching %r in %s. Items present: %s",
                    item_match, p, ", ".join(df["item"].head(20)))
        return None
    if len(hit) > 1:
        log.warning("%d items matched %r; using the first (%s). Narrow item_match "
                    "if that is wrong.", len(hit), item_match, hit.iloc[0]["item"])

    r = hit.iloc[0]
    return ItemWeight(
        item=str(r["item"]).strip(),
        rural=float(r["rural"]),
        urban=float(r["urban"]),
        combined=float(r["combined"]),
        source=str(p),
    )


def cpi_contribution(
    pct_change: float,
    weight: ItemWeight,
    sector: str = "combined",
) -> dict:
    """Contribution of an airfare move to headline CPI.

    pct_change is in percent (6.2 means +6.2%), not a fraction.
    """
    share = weight.share(sector)
    pp = share * pct_change
    return {
        "sector": sector,
        "item": weight.item,
        "item_weight_pct": getattr(weight, sector),
        "airfare_pct_change": round(pct_change, 3),
        "cpi_contribution_pp": round(pp, 5),
        "cpi_contribution_bps": round(pp * 100, 2),
        "share_of_transport_division": round(
            getattr(weight, sector) / CPI_2024_WEIGHTS["07_transport"][sector], 4
        ),
    }


def monthly_impact(monthly: pd.DataFrame, weight: ItemWeight,
                   sector: str = "combined") -> pd.DataFrame:
    """Attach a CPI contribution to each month of the APIx monthly series.

    Monthly deliberately, not daily. A daily APIx move multiplied by a monthly
    CPI weight is not a number that means anything.
    """
    out = monthly.copy()
    if "change_pct" not in out.columns:
        raise ValueError("expected a 'change_pct' column — pass result['monthly']")
    share = weight.share(sector)
    out["cpi_contribution_pp"] = (out["change_pct"] * share).round(5)
    out["cpi_contribution_bps"] = (out["cpi_contribution_pp"] * 100).round(2)
    out["item_weight_pct"] = getattr(weight, sector)
    out["basis"] = f"{weight.series}, {sector}"
    return out


def transport_weight_change() -> dict:
    """The 'transport matters more now' figure, for the deck."""
    new = CPI_2024_WEIGHTS["07_transport"]["combined"]
    old = CPI_2012_TRANSPORT_COMMUNICATION_COMBINED
    return {
        "cpi_2012_transport_and_communication": old,
        "cpi_2024_transport_division_07": new,
        "cpi_2024_information_communication_division_08": (
            CPI_2024_WEIGHTS["08_information_communication"]["combined"]),
        "pct_increase": round(100 * (new - old) / old, 1),
        "caveat": (
            "Not a like-for-like comparison: CPI 2012 grouped transport with "
            "communication, while COICOP 2018 splits them into divisions 07 "
            "and 08. Compare 8.796 against 6.394 only with that stated."
        ),
    }


def describe(contribution: dict) -> str:
    """One sentence, for the dashboard and the chat assistant."""
    bps = contribution["cpi_contribution_bps"]
    return (
        f"Airfares moved {contribution['airfare_pct_change']:+.2f}%. "
        f"At an item weight of {contribution['item_weight_pct']:.3f}% of the "
        f"CPI basket, that implies roughly {bps:+.1f} basis points on headline "
        f"CPI ({contribution['sector']}). Estimated impact using the APIx "
        f"basket, not a measurement of the official item index."
    )
