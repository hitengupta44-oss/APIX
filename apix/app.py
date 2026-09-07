"""APIx API service. Deploys to a Hugging Face Space (Docker, cpu-basic).

Endpoints NSO/RBI would consume are deliberately boring and cacheable:
    GET  /v1/index/daily?from=&to=
    GET  /v1/index/monthly
    GET  /v1/index/routes?date=
    GET  /v1/leadtime?route=
    GET  /v1/heatmap
    POST /v1/chat            (Groq-backed, grounded on the index tables)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import List, Optional

import httpx
import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("apix.app")

OUT = Path(os.environ.get("APIX_OUT_DIR", "out"))
GROQ_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
ALLOWED_ORIGINS = [o for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o]

app = FastAPI(title="APIx — Real-time Airfare Price Index", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_cache: dict[str, pd.DataFrame] = {}


def table(name: str) -> pd.DataFrame:
    if name not in _cache:
        p = OUT / f"apix_{name}.parquet"
        if not p.exists():
            raise HTTPException(503, f"table '{name}' not built yet; run the index job")
        _cache[name] = pd.read_parquet(p)
    return _cache[name]


@app.get("/health")
def health():
    built = sorted(p.stem.replace("apix_", "") for p in OUT.glob("apix_*.parquet"))
    return {"status": "ok", "tables": built, "groq": bool(GROQ_KEY)}


@app.post("/admin/reload")
def reload_tables(request: Request):
    if request.headers.get("x-admin-token") != os.environ.get("ADMIN_TOKEN", "\0"):
        raise HTTPException(401, "bad admin token")
    _cache.clear()
    return {"reloaded": True}


@app.get("/v1/index/daily")
def daily(
    date_from: Optional[date] = Query(None, alias="from"),
    date_to: Optional[date] = Query(None, alias="to"),
):
    d = table("daily").copy()
    d["quote_date"] = pd.to_datetime(d["quote_date"]).dt.date
    if date_from:
        d = d[d["quote_date"] >= date_from]
    if date_to:
        d = d[d["quote_date"] <= date_to]
    return {"series": json.loads(d.to_json(orient="records", date_format="iso"))}


@app.get("/v1/index/monthly")
def monthly():
    return {"series": json.loads(table("monthly").to_json(orient="records", date_format="iso"))}


@app.get("/v1/index/routes")
def routes(on: Optional[date] = None):
    d = table("route_daily").copy()
    d["quote_date"] = pd.to_datetime(d["quote_date"]).dt.date
    if on:
        d = d[d["quote_date"] == on]
    else:
        d = d[d["quote_date"] == d["quote_date"].max()]
    return {"routes": json.loads(d.to_json(orient="records", date_format="iso"))}


@app.get("/v1/leadtime")
def leadtime(route: Optional[str] = None):
    cells = table("cells").copy()
    if route:
        cells = cells[cells["route"] == route]
    curve = (
        cells.groupby(["route", "apw"])["price"]
        .median().reset_index().sort_values(["route", "apw"])
    )
    return {"curve": json.loads(curve.to_json(orient="records"))}


@app.get("/v1/heatmap")
def heatmap():
    cells = table("cells")
    pivot = cells.pivot_table(index="route", columns="apw", values="price", aggfunc="median")
    return {
        "routes": list(pivot.index),
        "windows": [int(c) for c in pivot.columns],
        "values": pivot.round(0).where(pivot.notna(), None).values.tolist(),
    }


@app.get("/v1/capacity")
def capacity(period: Optional[str] = None):
    """Supply-side context: is a fare move demand or seats?

    DGCA carrier statistics are national and monthly, so this frames the
    national monthly picture. It cannot explain a route-level daily move, and
    the response says so rather than letting a reader assume otherwise.
    """
    from .capacity import carrier_shares, hhi, industry_monthly

    monthly = industry_monthly()
    if monthly.empty:
        raise HTTPException(
            503,
            "No carrier statistics ingested. Download DGCA monthly traffic "
            "workbooks and run: python -m scripts.ingest_carrier_stats "
            "data/carriers/*.xlsx",
        )
    ts = pd.Timestamp(period) if period else None
    shares = carrier_shares(period=ts)
    return {
        "monthly": json.loads(monthly.to_json(orient="records", date_format="iso")),
        "carrier_shares": json.loads(
            shares.drop(columns=["period"]).to_json(orient="records")),
        "as_at": str(shares["period"].iloc[0].date()) if not shares.empty else None,
        "concentration": hhi(period=ts),
        "note": (
            "National monthly figures from DGCA. Use to contextualise monthly "
            "APIx movements, not to explain route-level daily changes. Load "
            "factor is the sharper signal: capacity falling while load factor "
            "rises is upward fare pressure; both falling is not."
        ),
    }


@app.get("/v1/cpi/official")
def cpi_official(
    state: str = "All India",
    sector: str = Query("combined", pattern="^(rural|urban|combined)$"),
):
    """Official MoSPI CPI General Index, for overlaying against APIx.

    General Index only — the dashboard export MoSPI publishes has no division
    or group breakdown, so this is headline CPI, not group 07.3 passenger
    transport. The comparison is still meaningful (airfares versus overall
    retail prices), but do not label it as an airfare-vs-airfare check.
    """
    p = Path(os.environ.get("APIX_DATA_DIR", "data")) / "cpi_general_index.csv"
    if not p.exists():
        raise HTTPException(
            503,
            "CPI series not ingested. Run: python -m scripts.ingest_cpi <xlsx>",
        )
    df = pd.read_csv(p)
    df = df[(df["state"] == state) & (df["sector"] == sector)]
    if df.empty:
        raise HTTPException(404, f"no CPI series for state={state!r} sector={sector!r}")
    return {
        "state": state,
        "sector": sector,
        "base": "2024=100",
        "coverage": "General Index (All Groups) — no division/group breakdown",
        "series": json.loads(
            df[["period", "index_value", "yoy_pct"]]
            .to_json(orient="records", date_format="iso")
        ),
    }


@app.get("/v1/cpi/benchmark")
def cpi_benchmark(
    group: str = "07.3",
    sector: str = Query("combined", pattern="^(rural|urban|combined)$"),
):
    """Official CPI group series — the benchmark APIx should be validated against.

    Group 07.3 Passenger transport services is the closest published official
    series to what APIx measures. It is broader than airfares (it includes rail,
    bus and taxi), so expect APIx to be more volatile and to diverge; the useful
    test is whether the two move together in direction and rough magnitude over
    months, not whether the levels match.

    Populate with: python -m scripts.ingest_cpi_release <release.pdf>
    """
    p = Path(os.environ.get("APIX_DATA_DIR", "data")) / "cpi_groups.csv"
    if not p.exists():
        raise HTTPException(
            503,
            "No CPI group series ingested. Download monthly press releases from "
            "mospi.gov.in and run: python -m scripts.ingest_cpi_release <pdf>",
        )
    df = pd.read_csv(p, dtype={"group_code": str})
    df = df[(df["group_code"] == group) & (df["sector"] == sector)]
    if df.empty:
        raise HTTPException(404, f"no series for group {group!r} sector {sector!r}")

    months = df["period"].nunique()
    return {
        "group_code": group,
        "group_name": df["group_name"].iloc[0],
        "sector": sector,
        "base": "2024=100",
        "months_available": months,
        "note": (
            "Broader than airfares — covers all passenger transport services. "
            "Compare direction and magnitude, not levels."
            + ("" if months >= 6 else
               " Too few months for a meaningful comparison yet; ingest more "
               "press releases.")
        ),
        "series": json.loads(
            df.sort_values("period")[["period", "index_value", "inflation_pct"]]
            .to_json(orient="records", date_format="iso")
        ),
    }


@app.get("/v1/cpi-impact")
def cpi_impact(sector: str = Query("combined", pattern="^(rural|urban|combined)$")):
    """What the latest APIx move implies for headline CPI.

    Returns 503 with a pointer rather than a number when the item weight has
    not been fetched. A zero or guessed weight here would produce a
    confident-looking figure that is simply wrong, and this is the one number
    most likely to be quoted out of context.
    """
    from .cpi_impact import (
        load_item_weight, monthly_impact, transport_weight_change,
    )

    weight = load_item_weight()
    if weight is None:
        raise HTTPException(
            503,
            "CPI air-fare item weight not configured. Fetch the CPI 2024 "
            "item-level weighting diagram from cpi.mospi.gov.in (Announcement "
            "tab) and save it as data/cpi_2024_weights.csv. "
            "See docs/data-sources.md.",
        )

    m = monthly_impact(table("monthly"), weight, sector=sector)
    return {
        "sector": sector,
        "item": weight.item,
        "item_weight_pct": getattr(weight, sector),
        "series": weight.series,
        "context": transport_weight_change(),
        "months": json.loads(
            m[["quote_date", "apix", "change_pct", "cpi_contribution_pp",
               "cpi_contribution_bps"]].to_json(orient="records", date_format="iso")
        ),
        "note": (
            "Estimated impact on headline CPI if the official air-fare item "
            "index moved as APIx did. APIx uses a different basket and cadence "
            "from the CPI item index, so this is not a measurement of the "
            "official series."
        ),
    }


# ----------------------------------------------------------------- chat
class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    messages: List[ChatMessage]


SYSTEM = """You are the analyst assistant for APIx, a real-time airfare price \
index for India built for NSO/MoSPI.

Rules:
- Answer only from the CONTEXT block. If the context does not contain the \
answer, say so plainly and suggest which endpoint would have it.
- Index values are relative to a base period = 100. A move from 100 to 106 is a \
6% rise versus base, not 6 rupees.
- Never present the index as a fare quote or booking advice.
- State clearly when a figure rests on placeholder weights.
- CPI contribution figures are ESTIMATES of what headline CPI would show if the \
official air-fare item index moved as APIx did. APIx has a different basket and \
cadence from the CPI item index. Never describe a CPI contribution as a \
measurement of official inflation, and never compute one yourself from a weight \
you were not given — if the context has no item weight, say the weight has not \
been configured."""


def _context() -> str:
    parts = []
    try:
        d = table("daily").tail(30)
        parts.append("RECENT DAILY APIx:\n" + d.round(2).to_string(index=False))
    except HTTPException:
        pass
    try:
        m = table("monthly").tail(12)
        parts.append("MONTHLY APIx:\n" + m.round(2).to_string(index=False))
    except HTTPException:
        pass
    try:
        r = table("route_daily")
        r = r[r["quote_date"] == r["quote_date"].max()]
        parts.append("LATEST ROUTE INDICES:\n" + r.round(2).to_string(index=False))
    except HTTPException:
        pass
    return "\n\n".join(parts) or "No index tables have been built yet."


@app.post("/v1/chat")
async def chat(req: ChatRequest):
    if not GROQ_KEY:
        raise HTTPException(503, "GROQ_API_KEY is not configured on this Space")
    payload = {
        "model": GROQ_MODEL,
        "temperature": 0.2,
        "max_tokens": 900,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "system", "content": f"CONTEXT\n{_context()}"},
            *[m.model_dump() for m in req.messages[-8:]],
        ],
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            json=payload,
        )
    if r.status_code != 200:
        log.error("groq %s: %s", r.status_code, r.text[:400])
        raise HTTPException(502, "upstream model error")
    return {"reply": r.json()["choices"][0]["message"]["content"]}
