"""Compliance layer.

Every outbound request in this project goes through `ComplianceGate`. A source
that cannot pass the gate does not run — there is no override flag, because an
index that feeds monetary policy has to be defensible in front of a legal
review, not just technically clever.

Three things are enforced:
  1. Authorisation basis. Each source declares *why* it is allowed to collect
     (licensed API, written permission, or a public page that robots.txt
     permits). "No stated basis" is not a valid basis.
  2. robots.txt, including Crawl-delay, re-checked every REFRESH seconds.
  3. Politeness: per-host token bucket, jittered delays, honouring 429 /
     Retry-After, and a hard daily request budget per host.

Deliberately NOT implemented: CAPTCHA solving, browser-fingerprint spoofing,
and residential-proxy rotation. Those exist to defeat a site's access controls;
using them would put the collection outside both the source's terms of service
and the ethical-scraping requirement in the problem statement. See
docs/legal-basis.md for the sanctioned alternatives.
"""
from __future__ import annotations

import logging
import random
import threading
import time
import urllib.robotparser as robotparser
from dataclasses import dataclass
from datetime import date
from typing import Dict, Optional
from urllib.parse import urlparse

log = logging.getLogger("apix.compliance")

USER_AGENT = (
    "APIx-ResearchBot/0.1 (+https://example.org/apix; "
    "official-statistics price collection; contact: apix-ops@example.org)"
)

ROBOTS_REFRESH_SECONDS = 6 * 3600

# Authorisation bases a source may declare.
BASIS_LICENSED_API = "licensed_api"        # contracted GDS / NDC / aggregator feed
BASIS_WRITTEN_PERMISSION = "written_permission"  # signed MoU or data-sharing letter
BASIS_PUBLIC_ROBOTS_OK = "public_robots_ok"      # public page, robots.txt allows it

VALID_BASES = {BASIS_LICENSED_API, BASIS_WRITTEN_PERMISSION, BASIS_PUBLIC_ROBOTS_OK}


class ComplianceError(RuntimeError):
    """Raised when a request may not be made. Never caught-and-ignored."""


@dataclass
class SourcePolicy:
    """Declared collection policy for one source. Lives in config/sources.yaml."""

    name: str
    basis: str
    min_delay_seconds: float = 5.0
    daily_request_budget: int = 500
    permission_ref: Optional[str] = None   # contract ID, MoU filename, ticket
    respects_robots: bool = True

    def validate(self) -> None:
        if self.basis not in VALID_BASES:
            raise ComplianceError(
                f"source '{self.name}': basis '{self.basis}' is not a recognised "
                f"authorisation basis (expected one of {sorted(VALID_BASES)})"
            )
        if self.basis in (BASIS_LICENSED_API, BASIS_WRITTEN_PERMISSION) and not self.permission_ref:
            raise ComplianceError(
                f"source '{self.name}': basis '{self.basis}' requires permission_ref "
                "pointing at the contract or MoU that authorises collection"
            )
        if self.min_delay_seconds < 1.0:
            raise ComplianceError(
                f"source '{self.name}': min_delay_seconds below 1s is not polite"
            )


class _TokenBucket:
    def __init__(self, min_delay: float) -> None:
        self.min_delay = min_delay
        self.last = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            gap = self.min_delay + random.uniform(0, self.min_delay * 0.3)
            sleep_for = self.last + gap - now
            if sleep_for > 0:
                time.sleep(sleep_for)
            self.last = time.monotonic()


class ComplianceGate:
    def __init__(self, policy: SourcePolicy) -> None:
        policy.validate()
        self.policy = policy
        self._robots: Dict[str, tuple[robotparser.RobotFileParser, float]] = {}
        self._buckets: Dict[str, _TokenBucket] = {}
        self._spend: Dict[tuple[str, date], int] = {}
        self._blocked_until: Dict[str, float] = {}

    # ------------------------------------------------------------------ robots
    def _robots_for(self, host_url: str) -> robotparser.RobotFileParser:
        now = time.time()
        cached = self._robots.get(host_url)
        if cached and now - cached[1] < ROBOTS_REFRESH_SECONDS:
            return cached[0]
        rp = robotparser.RobotFileParser()
        rp.set_url(f"{host_url}/robots.txt")
        try:
            rp.read()
        except Exception as exc:  # network failure -> fail closed
            raise ComplianceError(
                f"could not read robots.txt at {host_url}: {exc}. "
                "Refusing to collect without knowing the site's rules."
            ) from exc
        self._robots[host_url] = (rp, now)
        return rp

    # ------------------------------------------------------------------ public
    def check(self, url: str) -> None:
        """Raise ComplianceError if this URL must not be fetched right now."""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ComplianceError(f"unsupported scheme in {url!r}")
        host = parsed.netloc
        host_url = f"{parsed.scheme}://{host}"

        until = self._blocked_until.get(host, 0.0)
        if time.monotonic() < until:
            raise ComplianceError(
                f"{host} asked us to back off; blocked for another "
                f"{until - time.monotonic():.0f}s"
            )

        spent_key = (host, date.today())
        if self._spend.get(spent_key, 0) >= self.policy.daily_request_budget:
            raise ComplianceError(
                f"daily request budget of {self.policy.daily_request_budget} "
                f"exhausted for {host}"
            )

        if self.policy.basis == BASIS_PUBLIC_ROBOTS_OK and self.policy.respects_robots:
            rp = self._robots_for(host_url)
            if not rp.can_fetch(USER_AGENT, url):
                raise ComplianceError(
                    f"robots.txt at {host_url} disallows {url} for our user-agent. "
                    "Use a licensed feed for this source instead."
                )
            crawl_delay = rp.crawl_delay(USER_AGENT)
            if crawl_delay and crawl_delay > self.policy.min_delay_seconds:
                log.info("%s: honouring Crawl-delay of %ss", host, crawl_delay)
                self.policy.min_delay_seconds = float(crawl_delay)

    def throttle(self, url: str) -> None:
        """Block until it is polite to send this request."""
        host = urlparse(url).netloc
        bucket = self._buckets.setdefault(host, _TokenBucket(self.policy.min_delay_seconds))
        bucket.wait()
        self._spend[(host, date.today())] = self._spend.get((host, date.today()), 0) + 1

    def note_response(self, url: str, status: int, retry_after: Optional[str] = None) -> None:
        """Feed the response status back in so we can slow down when asked."""
        host = urlparse(url).netloc
        if status in (429, 503):
            delay = 900.0
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    pass
            self._blocked_until[host] = time.monotonic() + delay
            log.warning("%s returned %s; backing off %.0fs", host, status, delay)
        elif status == 403:
            self._blocked_until[host] = time.monotonic() + 86400
            log.error(
                "%s returned 403. Treating as a refusal of access and stopping "
                "collection from this host for 24h. Escalate to obtain a "
                "licensed feed rather than working around the block.",
                host,
            )

    def requests_remaining(self, host: str) -> int:
        return max(0, self.policy.daily_request_budget - self._spend.get((host, date.today()), 0))
