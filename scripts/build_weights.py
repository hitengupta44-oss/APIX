#!/usr/bin/env python3
"""Build APIx route weights from DGCA domestic city-pair passenger traffic.

Source: https://github.com/Vonter/india-aviation-traffic
        aggregated/domestic/city.csv  (parsed from DGCA monthly Table 3)
Licence: ODbL. Attribute DGCA. Verify against the DGCA PDFs before publishing —
this is a community parse of the primary source, not the primary source.

    python -m scripts.build_weights --months 12 --top 30
    python -m scripts.build_weights --refresh          # re-download first

Two decisions worth understanding before you defend this to judges:

1. DGCA reports each unordered city pair once, with traffic in both
   directions (PaxToCity2, PaxFromCity2). APIx prices *directional* routes,
   because DEL-BOM and BOM-DEL are separately priced products. So each DGCA
   row becomes two weighted routes.

2. Weights are a 12-month trailing sum, not a single month. A single month
   embeds that month's seasonality into a weight that then multiplies every
   future observation, which double-counts seasonality. Twelve months is also
   what NSO uses for CPI weight reference periods.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pandas as pd
import yaml

DGCA_CSV_URL = (
    "https://raw.githubusercontent.com/Vonter/india-aviation-traffic/"
    "main/aggregated/domestic/city.csv"
)

# DGCA city names -> IATA. The source is not clean: the same airport appears
# under several spellings, DGCA renames entries between releases, and multi-
# airport metros are reported separately. `normalise_city` below absorbs the
# drift so a rename does not silently drop a route from the basket.
CITY_TO_IATA = {
    "DELHI": "DEL", "MUMBAI": "BOM", "NAVI MUMBAI": "NMI",
    "BENGALURU": "BLR", "BANGALORE": "BLR", "HYDERABAD": "HYD",
    "KOLKATA": "CCU", "CHENNAI": "MAA", "AHMEDABAD": "AMD", "PUNE": "PNQ",
    "GUWAHATI": "GAU", "KOCHI": "COK", "COCHIN": "COK", "LUCKNOW": "LKO",
    "DABOLIM": "GOI", "MOPA": "GOX", "GOA": "GOI", "JAIPUR": "JAI",
    "BHUBANESWAR": "BBI", "PATNA": "PAT", "SRINAGAR": "SXR",
    "INDORE": "IDR", "CHANDIGARH": "IXC", "VARANASI": "VNS",
    "BAGDOGRA": "IXB", "COIMBATORE": "CJB", "NAGPUR": "NAG",
    "VISAKHAPATNAM": "VTZ", "VISHAKHAPATNAM": "VTZ", "RANCHI": "IXR",
    "RAIPUR": "RPR", "TRIVANDRUM": "TRV", "THIRUVANANTHAPURAM": "TRV",
    "AMRITSAR": "ATQ", "JAMMU": "IXJ", "PORT BLAIR": "IXZ",
    "AGARTALA": "IXA", "UDAIPUR": "UDR", "MANGALORE": "IXE",
    "MANGALURU": "IXE", "IMPHAL": "IMF", "BHOPAL": "BHO", "SURAT": "STV",
    "VADODARA": "BDQ", "MADURAI": "IXM", "VIJAYAWADA": "VGA",
    "DEHRADUN": "DED", "DEHRA DUN": "DED", "TIRUPATI": "TIR",
    "LEH": "IXL", "JODHPUR": "JDH", "DIBRUGARH": "DIB",
    "GORAKHPUR": "GOP", "KOZHIKODE": "CCJ", "CALICUT": "CCJ",
    "AURANGABAD": "IXU", "RAJKOT": "HSR", "JAMNAGAR": "JGA",
    "SILCHAR": "IXS", "AIZAWL": "AJL", "DIMAPUR": "DMU",
    "JORHAT": "JRH", "TEZPUR": "TEZ", "SHILLONG": "SHL",
    "BELGAUM": "IXG", "HUBLI": "HBX", "TIRUCHIRAPPALLI": "TRZ",
    "TUTICORIN": "TCR", "SALEM": "SXV", "PONDICHERRY": "PNY",
    "KANPUR": "KNU", "PRAYAGRAJ": "IXD", "ALLAHABAD": "IXD",
    "AGRA": "AGR", "GWALIOR": "GWL", "JABALPUR": "JLR",
    "BHUJ": "BHJ", "BHAVNAGAR": "BHU", "PORBANDAR": "PBD",
    "DHARAMSHALA": "DHM", "SHIMLA": "SLV", "KULLU": "KUU",
    "PANTNAGAR": "PGH", "BAREILLY": "BEK", "JHARSUGUDA": "JRG",
    "DURGAPUR": "RDP", "COOCH BEHAR": "COH", "SHIRDI": "SAG",
    "KOLHAPUR": "KLH", "NASHIK": "ISK", "SINDHUDURG": "SDW",
}

# Treat these as one commercial city. A traveller choosing DEL-Goa does not
# care which Goa airport; pricing them separately would split the weight in
# half and push a genuinely large route out of the basket. Same reasoning for
# Navi Mumbai, which opened in 2025 and now splits Mumbai traffic.
CITY_MERGE = {"GOX": "GOI", "NMI": "BOM"}

# DGCA writes the same city several ways across releases:
#   "GOA (DABOLIM, SOUTH GOA)", "MUMBAI (NAVI MUMBAI)", "MUMBAI (MUMBAI)"
# Strip the parenthetical, then try the qualifier as a fallback, so a rename
# degrades to the parent city rather than vanishing from the basket.
import re

_PAREN = re.compile(r"^([^(]+?)\s*\(([^)]*)\)\s*$")


def normalise_city(raw: str) -> str | None:
    if not isinstance(raw, str):
        return None
    name = raw.strip().upper()
    if name in CITY_TO_IATA:
        return CITY_TO_IATA[name]
    m = _PAREN.match(name)
    if not m:
        return None
    parent, qualifier = m.group(1).strip(), m.group(2).strip()
    # Qualifier first — it is more specific. "MUMBAI (NAVI MUMBAI)" should
    # resolve to Navi Mumbai, not to Mumbai.
    for token in [qualifier] + [t.strip() for t in qualifier.split(",")]:
        if token in CITY_TO_IATA:
            return CITY_TO_IATA[token]
    return CITY_TO_IATA.get(parent)


def download(dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {DGCA_CSV_URL}")
    subprocess.run(["curl", "-sSL", "-o", str(dest), DGCA_CSV_URL], check=True)
    return dest


def load(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["period"] = pd.to_datetime(
        df["Year"].astype(str) + "-" + df["Month"].astype(str).str.zfill(2) + "-01"
    )
    return df


def build(df: pd.DataFrame, months: int, top: int) -> tuple[pd.DataFrame, dict]:
    latest = df["period"].max()
    start = latest - pd.DateOffset(months=months - 1)
    window = df[df["period"] >= start].copy()

    for col in ("City1", "City2"):
        window[col] = window[col].str.strip().str.upper()
        window[col + "_iata"] = window[col].map(normalise_city)

    unmapped = (
        pd.concat([
            window.loc[window["City1_iata"].isna(), ["City1", "PaxToCity2"]]
                  .rename(columns={"City1": "city", "PaxToCity2": "pax"}),
            window.loc[window["City2_iata"].isna(), ["City2", "PaxFromCity2"]]
                  .rename(columns={"City2": "city", "PaxFromCity2": "pax"}),
        ])
        .groupby("city")["pax"].sum().sort_values(ascending=False)
    )

    mapped = window.dropna(subset=["City1_iata", "City2_iata"]).copy()
    for col in ("City1_iata", "City2_iata"):
        mapped[col] = mapped[col].replace(CITY_MERGE)
    mapped = mapped[mapped["City1_iata"] != mapped["City2_iata"]]

    # Split each unordered pair into two directional routes.
    fwd = mapped[["City1_iata", "City2_iata", "PaxToCity2"]].rename(
        columns={"City1_iata": "origin", "City2_iata": "destination", "PaxToCity2": "pax"})
    rev = mapped[["City2_iata", "City1_iata", "PaxFromCity2"]].rename(
        columns={"City2_iata": "origin", "City1_iata": "destination", "PaxFromCity2": "pax"})

    routes = (
        pd.concat([fwd, rev])
        .groupby(["origin", "destination"], as_index=False)["pax"].sum()
    )
    routes = routes[routes["pax"] > 0]
    routes["route"] = routes["origin"] + "-" + routes["destination"]

    total_mapped = routes["pax"].sum()
    total_all = window["PaxToCity2"].sum() + window["PaxFromCity2"].sum()

    basket = routes.nlargest(top, "pax").copy()
    # Weights renormalise over the basket, not over all traffic. The basket is
    # a *sample* of the market, so its weights must sum to 1 among themselves —
    # this is the standard CPI treatment of a representative item basket.
    basket["weight"] = basket["pax"] / basket["pax"].sum()
    basket["share_of_market"] = basket["pax"] / total_all
    basket = basket.sort_values("weight", ascending=False).reset_index(drop=True)

    meta = {
        "window_start": start.strftime("%Y-%m"),
        "window_end": latest.strftime("%Y-%m"),
        "months": months,
        "total_pax_all_pairs": float(total_all),
        "total_pax_mapped": float(total_mapped),
        "mapped_coverage": float(total_mapped / total_all),
        "basket_coverage": float(basket["pax"].sum() / total_all),
        "n_pairs_available": int(len(routes)),
        "unmapped_top": unmapped.head(10).round(0).to_dict(),
    }
    return basket, meta


def write_yaml(basket: pd.DataFrame, meta: dict, out: Path) -> None:
    doc = {
        "version": f"dgca-{meta['window_end']}-top{len(basket)}",
        "effective_from": f"{meta['window_end']}-01",
        "weight_source": (
            f"DGCA domestic city-pair passenger traffic, "
            f"{meta['window_start']} to {meta['window_end']} "
            f"({meta['months']}-month trailing sum). "
            f"Via github.com/Vonter/india-aviation-traffic (ODbL). "
            f"Basket covers {meta['basket_coverage']:.1%} of domestic pax."
        ),
        "coverage": {k: round(v, 6) if isinstance(v, float) else v
                     for k, v in meta.items() if k != "unmapped_top"},
        "routes": [
            {"route": r.route, "origin": r.origin, "destination": r.destination,
             "weight": round(float(r.weight), 6), "pax_12m": int(r.pax)}
            for r in basket.itertuples()
        ],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False, default_flow_style=False)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default="data/dgca_domestic_city.csv")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--months", type=int, default=12)
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--out", default="config/route_weights.yaml")
    args = p.parse_args()

    path = Path(args.csv)
    if args.refresh or not path.exists():
        download(path)

    basket, meta = build(load(path), args.months, args.top)
    write_yaml(basket, meta, Path(args.out))

    print(f"\nwindow: {meta['window_start']} to {meta['window_end']} "
          f"({meta['months']} months)")
    print(f"city-name mapping covers {meta['mapped_coverage']:.1%} of domestic pax")
    print(f"top-{args.top} basket covers {meta['basket_coverage']:.1%} of domestic pax")
    print(f"\n{basket[['route', 'pax', 'weight', 'share_of_market']].head(20).to_string(index=False)}")
    if meta["unmapped_top"]:
        print("\nlargest unmapped cities (add to CITY_TO_IATA to raise coverage):")
        for c, v in list(meta["unmapped_top"].items())[:8]:
            print(f"  {c:<28} {v:>12,.0f}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
