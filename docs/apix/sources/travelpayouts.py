"""Adapter for the Travelpayouts (Aviasales) Data API.

Free to access after registering in the Travelpayouts affiliate network — no
per-call charge. Returns real fares on real Indian carriers in rupees, which
after the July 2026 Amadeus Self-Service shutdown and Kiwi Tequila closing to
self-serve signups makes it one of very few free sources of genuine Indian
domestic fare data.

    Token: travelpayouts.com -> Tools -> API
    Env:   TRAVELPAYOUTS_TOKEN

## The limitation you must state in your methodology

Travelpayouts serves **cached** prices derived from Aviasales user search
history, not live shopping queries, and the cache is retained for up to seven
days. So a quote collected today may reflect a price seen several days ago.

This matters for APIx specifically, because the whole premise is capturing the
dynamic price a traveller faces at a point in time. A stale quote understates
volatility and blurs the advance-purchase curve — a fare cached three days ago
against a T+7 departure is really a T+10 observation.

The adapter therefore:
  * sets `collection_method="cached_api"` so cached and live quotes are never
    pooled by accident;
  * reads the API's own `expires_at` to estimate cache age and records the
    **effective** advance-purchase window, not the nominal one;
  * drops quotes staler than `max_cache_age_days` (default 2).

Do not silently mix this with a live feed in a published series. Either report
it as its own series, or state the cache lag prominently. An index whose inputs
are of unknown age is not one a statistical agency can adopt.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from ..models import CABIN_ECONOMY, FareQuote
from .base import HttpSource, apw_of, utcnow

log = logging.getLogger("apix.sources.travelpayouts")

# Carriers that actually operate Indian domestic scheduled services. Aviasales
# aggregates globally and will return codeshare or charter operators that no
# consumer can book on these sectors; including them would distort the index.
INDIAN_DOMESTIC_CARRIERS = {"6E", "AI", "IX", "QP", "SG", "9I", "S5", "IC"}


class TravelpayoutsSource(HttpSource):
    name = "ota:travelpayouts"
    collection_method = "cached_api"

    BASE_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"

    def __init__(self, policy, token_env: str = "TRAVELPAYOUTS_TOKEN",
                 currency: str = "inr", market: str = "in",
                 max_cache_age_days: int = 2,
                 restrict_to_indian_carriers: bool = True, **kw) -> None:
        super().__init__(policy, **kw)
        token = os.environ.get(token_env)
        if not token:
            raise RuntimeError(
                f"{token_env} is not set. Register at travelpayouts.com and "
                "take the token from Tools -> API."
            )
        self.token = token
        self.currency = currency
        self.market = market
        self.max_cache_age_days = max_cache_age_days
        self.restrict = restrict_to_indian_carriers
        self._stale_dropped = 0
        self._total_seen = 0

        log.info(
            "Travelpayouts serves CACHED prices from Aviasales search history, "
            "not live quotes. Quotes older than %d day(s) are dropped and the "
            "rest carry an effective (cache-adjusted) advance-purchase window.",
            max_cache_age_days,
        )

    def fetch(self, origin: str, destination: str,
              departure_date: date) -> List[FareQuote]:
        params = {
            "origin": origin,
            "destination": destination,
            "departure_at": departure_date.isoformat(),
            "one_way": "true",
            "direct": "false",
            "currency": self.currency,
            "market": self.market,
            "limit": 100,
            "sorting": "price",
            "token": self.token,
        }
        resp = self.get(self.BASE_URL, params=params)
        body = resp.json()
        if not body.get("success", False):
            log.warning("[%s] %s-%s: API reported failure: %s",
                        self.name, origin, destination, body.get("error"))
            return []

        ts = utcnow()
        out: List[FareQuote] = []
        for row in body.get("data", []) or []:
            q = self._parse(row, origin, destination, departure_date, ts)
            if q is not None:
                out.append(q)
        return out

    # ------------------------------------------------------------------ parse
    def _parse(self, row: dict, origin: str, destination: str,
               departure_date: date, ts: datetime) -> Optional[FareQuote]:
        self._total_seen += 1

        price = row.get("price")
        carrier = (row.get("airline") or "").upper().strip()
        if price is None or not carrier:
            return None

        if self.restrict and carrier not in INDIAN_DOMESTIC_CARRIERS:
            return None

        cache_age = self._cache_age_days(row, ts)
        if cache_age is not None and cache_age > self.max_cache_age_days:
            self._stale_dropped += 1
            return None

        # The nominal window is departure minus today. If the price was cached
        # two days ago, the traveller who saw it was really booking two days
        # further out, so the observation belongs in a later bucket.
        nominal = apw_of(ts, departure_date)
        effective = nominal + int(cache_age or 0)

        dep_local = None
        raw_dep = row.get("departure_at")
        if isinstance(raw_dep, str) and len(raw_dep) >= 16:
            dep_local = raw_dep[11:16]

        flight_no = row.get("flight_number")
        return FareQuote(
            quote_ts=ts,
            origin=origin,
            destination=destination,
            departure_date=departure_date,
            apw_days=effective,
            carrier=carrier,
            flight_number=f"{carrier}{flight_no}" if flight_no else None,
            cabin=CABIN_ECONOMY,
            stops=int(row.get("transfers") or 0),
            duration_min=row.get("duration_to") or row.get("duration"),
            departure_time_local=dep_local,
            total_fare=float(price),
            currency=self.currency.upper(),
            source=self.name,
            collection_method=self.collection_method,
        )

    def _cache_age_days(self, row: dict, ts: datetime) -> Optional[float]:
        """Estimate how old a cached price is.

        Aviasales returns `expires_at` rather than a cached-at timestamp, so
        age is inferred from how far the expiry sits from now against the
        documented seven-day retention. Approximate, and better than assuming
        every quote is fresh.
        """
        raw = row.get("expires_at")
        if not raw:
            return None
        try:
            expires = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        remaining = (expires - ts).total_seconds() / 86400.0
        age = 7.0 - remaining
        return max(0.0, min(age, 7.0))

    def stale_share(self) -> float:
        """Fraction of observations dropped for being too old. Report this."""
        return self._stale_dropped / self._total_seen if self._total_seen else 0.0
