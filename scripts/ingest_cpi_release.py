#!/usr/bin/env python3
"""Parse division- and group-level indices out of a MoSPI CPI press release.

    python -m scripts.ingest_cpi_release <release.pdf> [more.pdf ...]

Why this exists: the CPI dashboard XLSX export carries only the General Index.
The monthly press release PDF carries Annexure I (12 divisions) and Annexure II
(43 groups), and Annexure II is where **group 07.3 Passenger transport
services** lives — the closest official series to what APIx measures, and the
benchmark to validate against.

Run it over every monthly release you can download and you accumulate a real
07.3 time series. Each release adds one month, so back-fill by fetching past
releases from mospi.gov.in.

Outputs, appended and de-duplicated across runs:
    data/cpi_divisions.csv   period, division_code, division_name, sector,
                             index_value, inflation_pct
    data/cpi_groups.csv      period, group_code, group_name, sector,
                             index_value, inflation_pct
    data/cpi_general_monthly.csv  the Annexure IV back-series

Parses text, not tables, because MoSPI's PDF table structure varies between
releases while the numeric layout of the annexures has been stable. Wrapped
group names (a name split across two lines) are handled — the numbers always
sit on the last line of the row.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd

MONTHS = {m.lower()[:3]: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

NUM = r"(-?\d+\.\d+)"
NUM_ONE = re.compile(NUM)
# A run of exactly the six trailing figures: index r/u/c then inflation r/u/c.
# Any whitespace between figures. Requiring two-plus spaces matches the usual
# column alignment but breaks whenever a row is set tightly, and the failure is
# silent: the row becomes a name fragment and its figures land on the previous
# code.
NUM_RUN = re.compile(r"(?:\s+" + NUM + r"){6}\s*$")
# Division codes are two digits, group codes are two digits + one decimal.
CODE_AT_START = re.compile(r"^\s*(\d{2}(?:\.\d)?)(?=\s|$)")
# Lines that belong to the surrounding page, not to a table row.
PROSE = re.compile(r"[•:;]|Press Release|Page \d|Annexure|http|%\)|embargo", re.I)
# Column headers sit immediately above the first data row and would otherwise
# be glued onto its name ("Division name Food and beverages").
HEADER_LEAD = re.compile(r"^(?:Division|Group)\s+name\s+", re.I)
HEADER_WORDS = re.compile(
    r"^(Division|Group)\s+(name|code)$|^(Index|Inflation|Rural|Urban|Comb\.?)$", re.I)
# COICOP 2018 divisions actually present in CPI 2024. Codes outside this set
# are page numbers or serial numbers the layout happened to leave at line start.
VALID_DIVISIONS = {"01","02","03","04","05","06","07","08","09","10","11","12","13"}

# Annexure IV: "July-26*  108.34  107.45  107.94  4.84  3.96  4.45"
# Early months have index only, no inflation, because there is no year-ago base.
MONTH_ROW = re.compile(
    r"^\s*([A-Za-z]+)-(\d{2})\*?\s+" + NUM + r"\s+" + NUM + r"\s+" + NUM
    + r"(?:\s+" + NUM + r"\s+" + NUM + r"\s+" + NUM + r")?\s*$"
)

RELEASE_MONTH = re.compile(r"FOR\s+([A-Z]+),?\s+(\d{4})", re.I)


def pdf_text(path: Path) -> str:
    r = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"pdftotext failed on {path}. Install poppler-utils.")
    return r.stdout


def release_period(text: str, path: Path) -> pd.Timestamp | None:
    m = RELEASE_MONTH.search(text)
    if m:
        mon = MONTHS.get(m.group(1).lower()[:3])
        if mon:
            return pd.Timestamp(int(m.group(2)), mon, 1)
    print(f"  could not read the release month from {path.name}")
    return None


def parse_rows(text: str) -> list[tuple[str, str, list[float]]]:
    """Every code/name/6-number row, however MoSPI wrapped it.

    The annexures wrap three different ways in the same document:

        01 Food and beverages                    <- code + name, numbers below
                     108.97 109.33 ...  5.24     <- bare numbers

                 Housing, water, electricity,    <- name fragment
        04                                       <- code alone, mid-block
                 gas and other fuels
                     104.61 103.40 ...  2.16

        24. 07.3 Passenger transport services  105.24 ... 2.90   <- all one line

    So this accumulates a code and name fragments until six numbers appear,
    then emits. Trying to match this with per-line regexes is what dropped ten
    of the twelve divisions on the first pass.
    """
    rows: list[tuple[str, str, list[float]]] = []
    code: str | None = None
    parts: list[str] = []

    def flush(trailing: str, nums: list[float]) -> None:
        nonlocal code, parts
        if code is not None:
            name = " ".join(p for p in [*parts, trailing] if p).strip()
            name = re.sub(r"\s{2,}", " ", name)
            name = HEADER_LEAD.sub("", name).strip()
            # The page carries a "2026" watermark that pdftotext drops into
            # the text layer at arbitrary positions.
            name = re.sub(r"^(?:\d{4}\s+)+", "", name).strip()
            rows.append((code, name, nums))
        code, parts = None, []

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        nums = NUM_RUN.search(line)
        head = line[: nums.start()] if nums else line
        # Strip the serial number prefix ("24.") and capture a code.
        head = re.sub(r"^\s*\d+\.\s+", " ", head)
        m = CODE_AT_START.match(head)
        found_code = None
        if m:
            found_code = m.group(1)
            head = head[m.end():]
        head = head.strip()

        if found_code:
            if code is not None and parts and not nums:
                # A new code arrived before the previous row's numbers. The
                # previous row was a header or a wrapped fragment; drop it.
                parts = []
            code = found_code
            if nums:
                # Code, name and figures all on one line: the row is complete
                # in itself, so anything accumulated before it belongs to
                # something else (a section heading, a total row's label).
                parts = []

        if nums:
            values = [float(x) for x in NUM_ONE.findall(nums.group(0))]
            if len(values) == 6 and code is not None:
                flush(head, values)
                continue
            # Six numbers with no code is the "All India" total row; ignore.
            code, parts = None, []
            continue

        if head and not head.startswith(("(", "Note", "S.No")):
            parts.append(head)
            # A name fragment is short and label-like. Anything long, or
            # carrying sentence punctuation, is body prose from the page around
            # the table. Without this filter the parser swallows the entire
            # press release into the first division's name.
            parts = [p for p in parts
                     if len(p) <= 60 and not PROSE.search(p)
                     and not HEADER_WORDS.match(p)][-2:]

    return rows


def to_long(rows, period, kind: str) -> pd.DataFrame:
    recs = []
    for code, name, nums in rows:
        idx_r, idx_u, idx_c, inf_r, inf_u, inf_c = nums
        for sector, iv, inf in (("rural", idx_r, inf_r),
                                ("urban", idx_u, inf_u),
                                ("combined", idx_c, inf_c)):
            recs.append({
                "period": period,
                f"{kind}_code": code,
                f"{kind}_name": name,
                "sector": sector,
                "index_value": iv,
                "inflation_pct": inf,
            })
    return pd.DataFrame(recs)


def parse_general_series(text: str) -> pd.DataFrame:
    recs = []
    for line in text.splitlines():
        m = MONTH_ROW.match(line)
        if not m:
            continue
        mon = MONTHS.get(m.group(1).lower()[:3])
        if not mon:
            continue
        period = pd.Timestamp(2000 + int(m.group(2)), mon, 1)
        g = m.groups()
        for i, sector in enumerate(("rural", "urban", "combined")):
            recs.append({
                "period": period,
                "sector": sector,
                "index_value": float(g[2 + i]),
                "inflation_pct": float(g[5 + i]) if g[5 + i] else None,
            })
    return pd.DataFrame(recs)


def merge_append(new: pd.DataFrame, path: Path, keys: list[str]) -> pd.DataFrame:
    """Append to whatever is already on disk, newest wins on conflict.

    Provisional figures get revised — July is provisional in the July release
    and final in the August one — so a later parse must overwrite an earlier
    one for the same period."""
    if path.exists():
        old = pd.read_csv(path, parse_dates=["period"])
        new = pd.concat([old, new], ignore_index=True)
    return (new.drop_duplicates(subset=keys, keep="last")
            .sort_values(keys).reset_index(drop=True))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("pdfs", nargs="+")
    p.add_argument("--out-dir", default="data")
    a = p.parse_args()
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    div_all, grp_all, gen_all = [], [], []

    for f in a.pdfs:
        path = Path(f)
        text = pdf_text(path)
        period = release_period(text, path)
        if period is None:
            continue

        rows = parse_rows(text)
        divisions = [r for r in rows
                     if "." not in r[0] and r[0] in VALID_DIVISIONS and r[1]]
        groups = [r for r in rows
                  if "." in r[0] and r[0][:2] in VALID_DIVISIONS and r[1]]

        print(f"{path.name}: {period:%b %Y} — "
              f"{len(divisions)} divisions, {len(groups)} groups")

        if divisions:
            div_all.append(to_long(divisions, period, "division"))
        if groups:
            grp_all.append(to_long(groups, period, "group"))
        gen = parse_general_series(text)
        if not gen.empty:
            gen_all.append(gen)

    if div_all:
        d = merge_append(pd.concat(div_all, ignore_index=True),
                         out / "cpi_divisions.csv",
                         ["period", "division_code", "sector"])
        d.to_csv(out / "cpi_divisions.csv", index=False)
        print(f"\ncpi_divisions.csv: {len(d)} rows")

    if grp_all:
        g = merge_append(pd.concat(grp_all, ignore_index=True),
                         out / "cpi_groups.csv",
                         ["period", "group_code", "sector"])
        g.to_csv(out / "cpi_groups.csv", index=False)
        print(f"cpi_groups.csv: {len(g)} rows")

        pts = g[(g["group_code"] == "07.3") & (g["sector"] == "combined")]
        if not pts.empty:
            print("\ngroup 07.3 Passenger transport services (combined) — "
                  "the APIx benchmark:")
            print(pts[["period", "index_value", "inflation_pct"]]
                  .to_string(index=False))
        else:
            print("\n  group 07.3 not found — check the Annexure II layout")

    if gen_all:
        s = merge_append(pd.concat(gen_all, ignore_index=True),
                         out / "cpi_general_monthly.csv",
                         ["period", "sector"])
        s.to_csv(out / "cpi_general_monthly.csv", index=False)
        print(f"\ncpi_general_monthly.csv: {len(s)} rows, "
              f"{s['period'].min():%b %Y} to {s['period'].max():%b %Y}")

    print("\nEach release adds one month. Back-fill by downloading past "
          "releases from mospi.gov.in and passing them all at once.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
