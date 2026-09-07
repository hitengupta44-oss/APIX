#!/usr/bin/env python3
"""Ingest the MoSPI CPI dashboard export into a tidy series.

    python -m scripts.ingest_cpi "CPI_updated_July__2026_Dashboard_Data-12_08_2026.xlsx"

What this export actually contains, verified by reading it: the **General Index
(All Groups)** only, monthly Jan 2025 - Jul 2026, for All India and each
State/UT across rural, urban and combined. Base 2024=100.

What it does NOT contain, and what you still need separately:
  * division/group breakdown, so no group 07.3 Passenger transport services
  * item-level weights, so no air-fare weight

That means this file lets you chart APIx against *headline* CPI, which is a
real and useful comparison, but it cannot give you the airfare-specific
comparison or the CPI contribution figure. See docs/data-sources.md.

Output: data/cpi_general_index.csv with columns
    period, year, month, state, sector, index_value
plus data/cpi_state_inflation.csv for the state-wise y/y rates.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})


def _month_num(m) -> int | None:
    if pd.isna(m):
        return None
    return MONTHS.get(str(m).strip().title()) or MONTHS.get(str(m).strip().title()[:3])


def national(xl: pd.ExcelFile) -> pd.DataFrame:
    """The three all-India sheets, one per sector."""
    frames = []
    for sheet, sector in (("CPI- Rural ", "rural"),
                          ("CPI- Urban ", "urban"),
                          ("CPI Combined ", "combined")):
        if sheet not in xl.sheet_names:
            print(f"  warning: sheet {sheet!r} not found, skipping")
            continue
        d = xl.parse(sheet, header=1)
        d.columns = [str(c).strip() for c in d.columns]
        value_col = d.columns[-1]
        out = pd.DataFrame({
            "year": pd.to_numeric(d["Year"], errors="coerce"),
            "month_name": d["Month"],
            # The source spells All India two ways in the same column.
            "state": d["State"].astype(str).str.strip().str.title(),
            "description": d["Description"].astype(str).str.strip(),
            "sector": sector,
            "index_value": pd.to_numeric(d[value_col], errors="coerce"),
        })
        frames.append(out)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def statewise(xl: pd.ExcelFile) -> pd.DataFrame:
    """StateWise-CPI Index holds two tables side by side: inflation rates on
    the left, index levels on the right, separated by a blank column. Only the
    right-hand block is the index."""
    if "StateWise-CPI Index" not in xl.sheet_names:
        return pd.DataFrame()
    d = xl.parse("StateWise-CPI Index")
    d.columns = [str(c).strip() for c in d.columns]
    right = [c for c in d.columns if c.endswith(".1")]
    if not right:
        return pd.DataFrame()
    # Split positionally on the blank separator column, not by the ".1" suffix
    # pandas appends to duplicate headers. The state column arrives as
    # "Unnamed: 10" with no suffix, so a suffix filter silently drops it and
    # you get an index with no states in it.
    blanks = [i for i, c in enumerate(d.columns) if d[c].isna().all()]
    blk = d.iloc[:, (blanks[0] + 1) if blanks else 0:].copy()
    blk.columns = [c[:-2] if c.endswith(".1") else c for c in blk.columns]
    state_col = next((c for c in blk.columns
                      if c.startswith("Unnamed") or c.lower() == "state"), None)
    if state_col is None:
        return pd.DataFrame()
    blk = blk.rename(columns={state_col: "State", "Month": "month_name"})
    if "State" not in blk.columns:
        return pd.DataFrame()

    frames = []
    for sector in ("Rural", "Urban", "Combined"):
        if sector not in blk.columns:
            continue
        frames.append(pd.DataFrame({
            "year": pd.to_numeric(blk["Year"], errors="coerce"),
            "month_name": blk["month_name"],
            "state": blk["State"].astype(str).str.strip().str.title(),
            "description": blk.get("Description", "General Index (All Groups)"),
            "sector": sector.lower(),
            "index_value": pd.to_numeric(blk[sector], errors="coerce"),
        }))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def state_inflation(xl: pd.ExcelFile) -> pd.DataFrame:
    """Wide state-wise y/y inflation -> long."""
    sheet = "StateWise-InflationRate(%)"
    if sheet not in xl.sheet_names:
        return pd.DataFrame()
    d = xl.parse(sheet)
    d.columns = [str(c).strip() for c in d.columns]
    idcols = [c for c in ("Year", "Month") if c in d.columns]
    long = d.melt(id_vars=idcols, var_name="state", value_name="inflation_pct")
    long["month_num"] = long["Month"].map(_month_num)
    long = long.dropna(subset=["Year", "month_num", "inflation_pct"])
    long["period"] = pd.to_datetime(
        long["Year"].astype(int).astype(str) + "-"
        + long["month_num"].astype(int).astype(str).str.zfill(2) + "-01")
    return (long[["period", "state", "inflation_pct"]]
            .assign(state=lambda x: x["state"].str.strip().str.title())
            .sort_values(["period", "state"]).reset_index(drop=True))


def build(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    xl = pd.ExcelFile(path)
    print(f"sheets: {xl.sheet_names}")

    idx = pd.concat([national(xl), statewise(xl)], ignore_index=True)
    idx["month_num"] = idx["month_name"].map(_month_num)
    idx = idx.dropna(subset=["year", "month_num", "index_value"])
    idx["period"] = pd.to_datetime(
        idx["year"].astype(int).astype(str) + "-"
        + idx["month_num"].astype(int).astype(str).str.zfill(2) + "-01")
    idx["state"] = idx["state"].replace({"All India": "All India"})
    idx = (idx[["period", "state", "sector", "description", "index_value"]]
           .drop_duplicates(subset=["period", "state", "sector", "description"])
           .sort_values(["period", "state", "sector"])
           .reset_index(drop=True))

    # y/y on the national combined series, so the dashboard can show APIx
    # against headline inflation without recomputing it client-side.
    idx["yoy_pct"] = (
        idx.sort_values("period")
        .groupby(["state", "sector", "description"])["index_value"]
        .pct_change(12) * 100
    ).round(3)

    return idx, state_inflation(xl)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("xlsx")
    p.add_argument("--out-dir", default="data")
    a = p.parse_args()

    idx, infl = build(Path(a.xlsx))
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    idx.to_csv(out / "cpi_general_index.csv", index=False)
    if not infl.empty:
        infl.to_csv(out / "cpi_state_inflation.csv", index=False)

    nat = idx[idx["state"] == "All India"]
    print(f"\n{len(idx):,} index rows, {idx['state'].nunique()} states, "
          f"{idx['period'].min():%Y-%m} to {idx['period'].max():%Y-%m}")
    print(f"descriptions present: {sorted(idx['description'].unique())}")
    print(f"\nAll India, combined, last 6 months:")
    print(nat[nat["sector"] == "combined"]
          .tail(6)[["period", "index_value", "yoy_pct"]].to_string(index=False))
    print("\nNote: this export is the General Index only. No division/group "
          "breakdown, so group 07.3 (passenger transport) is NOT available "
          "here, and neither are item weights.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
