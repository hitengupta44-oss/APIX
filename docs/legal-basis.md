# Collection policy and legal basis

## The design decision

The problem statement asks for scraping that handles "dynamic CAPTCHAs,
anti-bot measures, IP rotation" while "remaining compliant with the robots.txt
and terms of service of source websites". Those two halves pull against each
other. A CAPTCHA is a site telling you it does not consent to automated access;
rotating IPs to get past one is not compliance with terms of service, it is
evasion of the mechanism that enforces them.

This project resolves the tension in the direction that survives review. It
does not implement CAPTCHA solving, browser-fingerprint spoofing, or
residential-proxy rotation. `compliance.py` fails closed instead: a 403 stops
collection from that host for 24 hours and raises an escalation, rather than
retrying from a different address.

That is not only the safer choice, it is the stronger submission. This index is
meant to feed RBI monetary policy. Anything built on collection that a source
could characterise as unauthorised access is a liability the moment it matters
— and Indian airlines and OTAs have litigated over exactly this. Compare
*InterGlobe Aviation (IndiGo) v. Ticketstoyou / cheapticketsindia*, where the
Delhi High Court restrained an operator from scraping IndiGo's booking system.
An MoSPI-badged system with a proxy-rotation module in the repo is not a system
MoSPI can adopt.

Say this out loud to the judges. "We read the requirement, identified that two
clauses conflict, and resolved it toward the one that lets a ministry actually
deploy this" is a better answer than a CAPTCHA solver.

## Source hierarchy, in order of preference

**1. Licensed offer APIs — carry the bulk of the basket.**
Duffel, Amadeus Self-Service, Kiwi Tequila, Travelport. Same live dynamic
inventory a consumer sees, delivered under contract. `AggregatorApiSource` is
written against Duffel's shape; Amadeus needs only a different `_parse_offer`.
Free sandbox tiers are enough for a prototype, and "we have a credentialed path
to production" is exactly what a ministry wants to hear.

**2. Airline/OTA data partnerships.** For a government statistical programme
this is realistic in a way it is not for a startup. NSO can request a fare feed
under the Collection of Statistics Act, 2008, which gives MoSPI statutory
power to require returns from entities in a notified sector. Note this in the
deck: the production version of APIx probably does not scrape at all, and your
architecture should make that swap a config change. It does — flip `enabled` in
`basket.yaml`.

**3. Permitted public pages.** Some sources allow automated access to fare
pages in robots.txt. Where they do, `HttpSource` collects them politely:
identifiable user-agent with contact address, honoured `Crawl-delay`, per-host
budget, backoff on 429/503. Re-check robots.txt every six hours — permissions
change, and "it was allowed in October" is not a defence.

**4. Replay archive.** For development and demos. Use this by default so you
are not generating live traffic while iterating on a chart. It also makes the
index reproducible, which matters more than it sounds: an official statistic
you cannot regenerate from stored inputs cannot be audited.

## Operating rules

- One user-agent, honestly identifying the project, with a working contact.
- Per-host daily request budget, set in config, enforced in code.
- Randomised jitter on delays — to spread load, not to look human.
- Collect once daily per (route × window × source). Roughly 20 routes × 5
  windows × 2 sources = 200 requests/day. That is a rounding error in an
  airline's traffic and nothing anyone will object to.
- Store only what the index needs. Publish derived statistics, never
  redistribute raw fare tables. Note the schema enforces this: `fare_quotes`
  has no anon RLS policy, so the public API cannot reach it.
- Log every run in `collection_runs` with request counts. If a source ever
  asks what you did, you can answer precisely.

## What to write in your submission

State the basis for each source explicitly. `SourcePolicy` requires a
`permission_ref` for licensed and permission-based sources and will refuse to
construct without one, so the code cannot drift away from the documentation.
