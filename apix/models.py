"""Canonical schema for a single airfare quote observation."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from typing import Optional

# Advance-purchase windows mandated by the problem statement.
APW_WINDOWS = [1, 7, 15, 30, 45]

CABIN_ECONOMY = "ECONOMY"
CABIN_PREMIUM = "PREMIUM_ECONOMY"
CABIN_BUSINESS = "BUSINESS"


@dataclass
class FareQuote:
    """One priced itinerary, as seen by a consumer at a point in time.

    Everything the index needs must be derivable from this record alone, so
    that the index module never has to know which source produced the row.
    """

    # --- identity of the observation ---
    quote_ts: datetime          # when we observed the price (UTC)
    origin: str                 # IATA, e.g. "DEL"
    destination: str            # IATA, e.g. "BOM"
    departure_date: date
    apw_days: int               # departure_date - quote_ts.date()

    # --- product being priced ---
    carrier: str                # IATA airline code, e.g. "6E"
    flight_number: Optional[str] = None
    cabin: str = CABIN_ECONOMY
    fare_family: Optional[str] = None   # "SAVER", "FLEXI", ...
    stops: int = 0
    duration_min: Optional[int] = None
    departure_time_local: Optional[str] = None

    # --- money (all INR, per adult, one-way) ---
    total_fare: Optional[float] = None
    base_fare: Optional[float] = None
    taxes_and_fees: Optional[float] = None
    udf: Optional[float] = None            # user development fee
    convenience_fee: Optional[float] = None
    currency: str = "INR"

    # --- availability ---
    seats_available: Optional[int] = None
    sold_out: bool = False

    # --- provenance ---
    source: str = ""            # "airline:6E", "ota:xyz", "gds:duffel", "replay:kaggle"
    source_url: Optional[str] = None
    collection_method: str = ""  # "api" | "html" | "replay"
    raw_payload_hash: Optional[str] = None

    quote_id: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.origin = self.origin.upper().strip()
        self.destination = self.destination.upper().strip()
        self.carrier = self.carrier.upper().strip()
        if self.quote_ts.tzinfo is None:
            self.quote_ts = self.quote_ts.replace(tzinfo=timezone.utc)
        self.quote_id = self._fingerprint()

    def _fingerprint(self) -> str:
        """Stable ID so re-running a scrape does not duplicate rows."""
        key = "|".join(
            str(x)
            for x in (
                self.quote_ts.date().isoformat(),
                self.origin,
                self.destination,
                self.departure_date.isoformat(),
                self.carrier,
                self.flight_number or "",
                self.cabin,
                self.fare_family or "",
                self.source,
            )
        )
        return hashlib.sha1(key.encode()).hexdigest()[:20]

    @property
    def route(self) -> str:
        return f"{self.origin}-{self.destination}"

    def to_row(self) -> dict:
        d = asdict(self)
        d["quote_id"] = self.quote_id
        d["route"] = self.route
        d["quote_ts"] = self.quote_ts.isoformat()
        d["departure_date"] = self.departure_date.isoformat()
        return d
