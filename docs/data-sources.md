# Where to get each dataset

Split into what the repo already handles and what a human has to fetch. Verified
September 2026 — government portals move things, so if a link 404s the
navigation path below it is the durable route.

---

## Already in the repo — do nothing

| Dataset | Location | Refresh |
|---|---|---|
| DGCA domestic city-pair traffic | `data/dgca_domestic_city.csv` | `python -m scripts.build_weights --refresh` |
| Route weights | `config/route_weights.yaml` | same command, or monthly workflow |
| Event calendar | `data/event_calendar.csv` | `python -m scripts.build_calendar` |
| CPI division weights | `docs/data-gaps.md` (transcribed) | annual at most |

`--refresh` pulls straight from
`raw.githubusercontent.com/Vonter/india-aviation-traffic/main/aggregated/domestic/city.csv`.
No login, no key.

---

## Fetch these by hand

### 1. CPI air-fare item weight — 5 minutes, highest value on this list

**Where:** `https://www.cpi.mospi.gov.in` → **Announcement** tab
Mirror: `https://www.mospi.gov.in` → Publications/Announcements

The site blocks automated fetching, so open it in a browser. You want the CPI
2024 weighting diagram at **item level** (All India, Rural/Urban/Combined).
Look for the item under division 07, group **07.3 Passenger transport
services** — likely named "Air fare" or "Air fare (normal/economy)".

Why it matters more than anything else here: it converts APIx into a CPI
statement. `airfare_item_weight × APIx % change = basis points on headline
CPI`. That single sentence is the most quotable output your project can
produce, and you cannot write it without this number.

While you are there, grab the full item-weight table. Put it in
`data/cpi_2024_weights.csv` and I will wire the CPI-impact calculation in.

**Monthly index values** (for charting APIx against official 07.3):
`https://esankhyiki.mospi.gov.in/macroindicators?product=cpi`
This one has a proper data explorer with CSV export and an API.

### 2. DGCA monthly average fare — read this before you plan around it

**Warning.** Your problem statement says to back-test against "publicly
available DGCA monthly average-fare data". I could not confirm that such a
series is published.

What DGCA actually runs is a **Tariff Monitoring Unit** that checks fares on
78 domestic routes each month against the bands airlines declare themselves. In
a July 2026 Lok Sabha reply the government described it as a compliance check —
confirming airlines do not exceed their own declared bands — not a published
average-fare statistic. Under Air Transport Circular 02 of 2010, airlines
publish tariff sheets on their own websites; DGCA verifies against those.

So there may be no monthly average-fare series to back-test against.

Three moves, in order:

1. **Check the monthly reports yourself.** Navigate:
   `dgca.gov.in` → Data and Reports → Aviation Data and Statistics →
   Air Transport → Domestic Air Transport → **Monthly Statistics**.
   Deep links redirect to the homepage because the portal is a JS app, so
   click through. Look for a fare or tariff table alongside the traffic tables.
2. **File an RTI** if it is not there. DGCA's RTI page is in the top nav. A
   student project asking for the Tariff Monitoring Unit's published outputs is
   a clean, cheap request, and the reply is itself citable evidence for your
   report. Do this in week one — RTI takes 30 days.
3. **Have a fallback benchmark ready.** Airline quarterly investor
   presentations (IndiGo, SpiceJet) publish yield and RASK per available seat
   kilometre. Yield is revenue per passenger-km, which is a defensible proxy
   for average fare and is genuinely public. Correlating APIx against IndiGo
   yield is a weaker claim than DGCA average fare, but it is a real validation
   and you can actually get it.

Tell the judges you checked. "The benchmark named in the PS does not appear to
exist as a public series; here is the RTI, and here is the proxy we validated
against instead" is a much stronger answer than a missing chart.

### 3. ATF prices

**Best for a dated back-series:**
`https://www.data.gov.in/resource/effective-date-wise-prices-aviation-turbine-fuel-atf-indian-oil-corporation-limited-iocl`
IOCL ATF for domestic airlines at New Delhi, 2022-04 to 2025-03. CSV/JSON
download; free API key from data.gov.in if you want it automated.

**Current metro prices:** `https://iocl.com/atf-domestic-airlines-on-international`
Delhi, Mumbai, Kolkata, Chennai. Revised on the 1st of each month.

**The citation to use in your report:** PPAC, `https://ppac.gov.in` — this is
the source MoSPI itself uses for fuel prices in CPI, which makes it the
defensible one to cite even if you scrape the numbers elsewhere. Historical
data needs a free registration.

Reference points as of 1 March 2026: Delhi ₹96,638/KL (+5.7% m/m), Mumbai
₹90,452, Kolkata ₹99,587, Chennai ₹1,00,280.

### 4. UDF and ASF, with effective dates

**UDF:** AERA (`https://aera.gov.in`) → Tariff Orders. One order per airport
per control period. Tedious — you need DEL, BOM, BLR, HYD, MAA, CCU, AMD, COK
at minimum, which is the eight airports covering most of your basket.

**ASF:** Bureau of Civil Aviation Security / MoCA notifications. Currently
₹236 per departing domestic passenger, but confirm and date it.

Record the **effective date** with every value, not just the amount. When UDF
changes at BLR, every fare decomposition before that date used a different
constant, and undated values will break your ex-tax back-series silently at the
change point. Put them in `data/airport_charges.csv` as
`airport, charge_type, amount_inr, effective_from, effective_to, source_url`
and I will replace the hardcoded table in `normalize.py`.

Lowest priority on this list — only needed for the ex-tax series, not the
headline index.

### 5. Aggregator API credential — needed before you collect anything

Duffel (`https://duffel.com`) or Amadeus Self-Service
(`https://developers.amadeus.com`). Both have free sandbox tiers with instant
signup. Set as `AGGREGATOR_API_TOKEN` and flip `enabled: true` on the
`gds:aggregator` source in `config/basket.yaml`.

Without this the collector only has the replay archive and you are not
gathering new data. Given the 30-day back-test needs 30 days of wall clock,
this is the actual critical path item.

---

## Suggested order

1. **Today:** aggregator sandbox key, enable the source, start the daily job.
   Every day you delay is a day off the back-test.
2. **This week:** CPI item weight (5 min). File the DGCA RTI.
3. **Next:** ATF from data.gov.in.
4. **When you need the ex-tax series:** UDF/AERA.

Paste any of these in and I will wire them into the pipeline.
