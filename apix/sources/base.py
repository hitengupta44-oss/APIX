"""Source adapters.

A source turns a (route, departure_date) request into FareQuote records. The
collector does not care whether that came from a licensed API, a permitted HTML
page, or a replayed archive — which is what lets you swap collection methods
without touching the index code.
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from typing import Iterable, List, Optional

import requests

from ..compliance import USER_AGENT, ComplianceError, ComplianceGate, SourcePolicy
from ..models import FareQuote

log = logging.getLogger("apix.sources")


class Source(ABC):
    name: str = "abstract"
    collection_method: str = "abstract"

    def __init__(self, policy: SourcePolicy) -> None:
        self.policy = policy
        self.gate = ComplianceGate(policy)

    @abstractmethod
    def fetch(self, origin: str, destination: str, departure_date: date) -> List[FareQuote]:
        ...

    def collect(
        self, origin: str, destination: str, departure_date: date
    ) -> List[FareQuote]:
        """Wrapper that turns adapter failures into empty results, not crashes.

        A dead source must degrade the index (fewer observations in that cell),
        never abort the whole collection run.
        """
        try:
            quotes = self.fetch(origin, destination, departure_date)
        except ComplianceError as exc:
            log.warning("[%s] blocked by compliance gate: %s", self.name, exc)
            return []
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] %s-%s %s failed: %s", self.name, origin, destination, departure_date, exc)
            return []
        log.info("[%s] %s-%s dep=%s -> %d quotes", self.name, origin, destination, departure_date, len(quotes))
        return quotes


class HttpSource(Source):
    """Base for anything that makes HTTP calls. Handles the polite plumbing.

    Note there is no proxy-rotation or fingerprint-spoofing hook here, by
    design. If a site blocks this user-agent, the correct response is to obtain
    a feed, not to disguise the collector.
    """

    collection_method = "html"

    def __init__(self, policy: SourcePolicy, timeout: float = 30.0) -> None:
        super().__init__(policy)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-IN,en;q=0.9",
                "From": "apix-ops@example.org",
            }
        )

    def get(self, url: str, **kwargs) -> requests.Response:
        self.gate.check(url)
        self.gate.throttle(url)
        resp = self.session.get(url, timeout=self.timeout, **kwargs)
        self.gate.note_response(url, resp.status_code, resp.headers.get("Retry-After"))
        resp.raise_for_status()
        return resp

    def post(self, url: str, **kwargs) -> requests.Response:
        self.gate.check(url)
        self.gate.throttle(url)
        resp = self.session.post(url, timeout=self.timeout, **kwargs)
        self.gate.note_response(url, resp.status_code, resp.headers.get("Retry-After"))
        resp.raise_for_status()
        return resp


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def apw_of(quote_ts: datetime, departure_date: date) -> int:
    return (departure_date - quote_ts.date()).days
