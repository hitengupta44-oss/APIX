"""Adapter for a licensed flight-offer API (Duffel / Amadeus Self-Service / NDC).

This is the recommended *primary* source for APIx and should carry most of the
basket. It returns the same live, dynamically-priced inventory a consumer sees,
but under a contract that permits programmatic access — so there is no
CAPTCHA to defeat and no ToS exposure for MoSPI.

The response shape below follows Duffel's offer-request API. Amadeus's
`/v2/shopping/flight-offers` maps onto the same fields with a different JSON
path; keep the mapping in `_parse_offer` and the rest of the pipeline is
unchanged.
"""
from __future__ import annotations

import logging
import os
from datetime import date
from typing import List, Optional

from ..models import CABIN_ECONOMY, FareQuote
from .base import HttpSource, apw_of, utcnow

log = logging.getLogger("apix.sources.aggregator")

CABIN_MAP = {
    "economy": "ECONOMY",
    "premium_economy": "PREMIUM_ECONOMY",
    "business": "BUSINESS",
    "first": "BUSINESS",
}

# Duffel's test mode serves its own synthetic airline, "Duffel Airways" (ZZ),
# whose schedules and prices are explicitly not realistic. Amadeus's test
# environment behaves similarly with cached subsets.
#
# A sandbox fare is worse than no fare: it looks like data, flows through the
# whole pipeline, and produces an index that appears to work. So quotes from
# these carriers are tagged and, by default, dropped before they reach the
# cleaner. Set APIX_ALLOW_SANDBOX=1 to keep them when you are deliberately
# testing the plumbing.
SANDBOX_CARRIERS = {"ZZ"}


class SandboxDataError(RuntimeError):
    """Raised when a source returns only synthetic fares."""


class AggregatorApiSource(HttpSource):
    name = "gds:aggregator"
    collection_method = "api"

    BASE_URL = "https://api.duffel.com/air"

    def __init__(self, policy, token_env: str = "AGGREGATOR_API_TOKEN", **kw) -> None:
        super().__init__(policy, **kw)
        token = os.environ.get(token_env)
        if not token:
            raise RuntimeError(
                f"{token_env} is not set. The aggregator adapter needs the API "
                "credential issued under your data licence."
            )
        self.allow_sandbox = os.environ.get("APIX_ALLOW_SANDBOX") == "1"
        self._sandbox_only_runs = 0

        if token.startswith("duffel_test_"):
            log.warning(
                "AGGREGATOR_API_TOKEN is a Duffel TEST token. Test mode serves "
                "the synthetic carrier 'Duffel Airways' (ZZ) with prices and "
                "schedules that Duffel states are not realistic. Useful for "
                "verifying the pipeline; useless as index input. Synthetic "
                "quotes will be dropped unless APIX_ALLOW_SANDBOX=1."
            )

        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Duffel-Version": "v2",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def fetch(self, origin: str, destination: str, departure_date: date) -> List[FareQuote]:
        payload = {
            "data": {
                "slices": [
                    {
                        "origin": origin,
                        "destination": destination,
                        "departure_date": departure_date.isoformat(),
                    }
                ],
                "passengers": [{"type": "adult"}],
                "cabin_class": "economy",
                "max_connections": 1,
            }
        }
        resp = self.post(
            f"{self.BASE_URL}/offer_requests?return_offers=true", json=payload
        )
        body = resp.json().get("data", {})
        ts = utcnow()
        quotes: List[FareQuote] = []
        for offer in body.get("offers", []):
            q = self._parse_offer(offer, origin, destination, departure_date, ts)
            if q is not None:
                quotes.append(q)

        return self._reject_sandbox(quotes, origin, destination)

    def _reject_sandbox(self, quotes: List[FareQuote], origin: str,
                        destination: str) -> List[FareQuote]:
        """Drop synthetic fares unless explicitly allowed.

        Reported loudly rather than silently, because "the collector ran and
        stored 150 quotes" is indistinguishable from success until someone
        notices every carrier is ZZ — by which point it may be in a chart.
        """
        if not quotes:
            return quotes
        real = [q for q in quotes if q.carrier not in SANDBOX_CARRIERS]
        fake = len(quotes) - len(real)

        if fake and not self.allow_sandbox:
            log.warning(
                "[%s] %s-%s: dropped %d/%d synthetic sandbox quotes. Test-mode "
                "fares are not realistic and must not enter the index. Use a "
                "live credential, or set APIX_ALLOW_SANDBOX=1 to keep them for "
                "plumbing tests.",
                self.name, origin, destination, fake, len(quotes),
            )
            self._sandbox_only_runs += 1
            if self._sandbox_only_runs == 5 and not real:
                log.error(
                    "[%s] five consecutive route requests returned only "
                    "synthetic fares. Your token is almost certainly a test "
                    "token (duffel_test_...). Nothing usable will be "
                    "collected until you switch to a live credential.",
                    self.name,
                )
            return real
        if fake and self.allow_sandbox:
            for q in quotes:
                if q.carrier in SANDBOX_CARRIERS:
                    q.source = f"{self.name}:SANDBOX"
        if real:
            self._sandbox_only_runs = 0
        return quotes

    # ------------------------------------------------------------------ parse
    def _parse_offer(self, offer, origin, destination, departure_date, ts) -> Optional[FareQuote]:
        slices = offer.get("slices") or []
        if not slices:
            return None
        segments = slices[0].get("segments") or []
        if not segments:
            return None
        first = segments[0]

        total = _num(offer.get("total_amount"))
        base = _num(offer.get("base_amount"))
        tax = _num(offer.get("tax_amount"))
        if total is None:
            return None
        # Some carriers report base+tax but not total, or vice-versa.
        if base is None and tax is not None:
            base = total - tax
        if tax is None and base is not None:
            tax = total - base

        cabin_raw = (first.get("passengers") or [{}])[0].get("cabin_class", "economy")

        return FareQuote(
            quote_ts=ts,
            origin=origin,
            destination=destination,
            departure_date=departure_date,
            apw_days=apw_of(ts, departure_date),
            carrier=(offer.get("owner") or {}).get("iata_code", "??"),
            flight_number=f"{(first.get('marketing_carrier') or {}).get('iata_code','')}"
            f"{first.get('marketing_carrier_flight_number','')}" or None,
            cabin=CABIN_MAP.get(cabin_raw, CABIN_ECONOMY),
            fare_family=(offer.get("passengers") or [{}])[0].get("fare_type"),
            stops=max(0, len(segments) - 1),
            duration_min=_iso_minutes(slices[0].get("duration")),
            departure_time_local=(first.get("departing_at") or "")[11:16] or None,
            total_fare=total,
            base_fare=base,
            taxes_and_fees=tax,
            currency=offer.get("total_currency", "INR"),
            seats_available=offer.get("available_services") and None,
            source=self.name,
            collection_method=self.collection_method,
        )


def _num(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _iso_minutes(dur: Optional[str]) -> Optional[int]:
    """Parse an ISO-8601 duration like 'PT2H15M' into minutes."""
    if not dur or not dur.startswith("PT"):
        return None
    hours = minutes = 0
    num = ""
    for ch in dur[2:]:
        if ch.isdigit():
            num += ch
        elif ch == "H":
            hours = int(num or 0)
            num = ""
        elif ch == "M":
            minutes = int(num or 0)
            num = ""
        else:
            num = ""
    return hours * 60 + minutes
