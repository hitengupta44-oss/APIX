# APIx dashboard — design plan

## Subject
A statistical instrument for MoSPI/RBI economists, not a travel product. The
audience reads CPI releases for a living. Its job: let someone see whether
airfares moved, on which routes, at which booking horizon, and say so with
enough provenance that they'd repeat the number in a meeting.

That rules out the travel-app vernacular entirely — no plane icons, no
boarding-pass motifs, no sky gradients. The visual reference is a statistical
release: RBI bulletins, ONS releases, Fed FRED charts.

## Rejected first pass
My instinct was dark navy, one cyan accent, index number in a huge gradient
hero, metric cards in a row. That's the generic analytics-dashboard default and
it says nothing about official statistics. Specifically dropped: near-black
background with a single acid accent, identical rounded cards with soft grey
shadows, ALL-CAPS eyebrow labels.

## Colour — paper, not screen
Official statistics are read on paper and in print-derived PDFs. Base is a
warm neutral paper tone with ink-dark text, and the only saturated colour is
carried by the data itself.

  --paper      #FBFAF7   page
  --ink        #1A1D21   primary text
  --ink-soft   #5C6169   secondary text
  --rule       #DFDCD4   hairlines, table rules
  --up         #A4341F   fares rising (deep vermilion, print-red)
  --down       #1F5E4A   fares falling (deep green)
  --provisional #B8892B  imputed/provisional data

Deliberately no brand accent. In a price index, colour means direction of
price movement and nothing else. A decorative accent would compete with the
one signal that matters.

## Type
Source Serif 4 for numerals and headings; IBM Plex Sans for UI and labels.
Serif numerals are the tell of a statistical publication rather than a SaaS
dashboard, and Source Serif has proper lining figures. Index values get
tabular-nums so columns align down the page.

Type scale (1.25): 12 / 14 / 16 / 20 / 25 / 31 / 39.
Headline index value at 39px serif, everything else quiet.

## Layout
Single column, max 1100px, left-aligned. Not a card grid — a release document
that happens to be live. Sections separated by hairline rules, not shadows.

  ┌────────────────────────────────────────────┐
  │ APIx            base 2024=100 · 07 Sep 2026│
  ├────────────────────────────────────────────┤
  │  106.4                                     │  ← hero: the number, serif
  │  +2.1% on yesterday   30 routes · 94.8% cov│
  ├────────────────────────────────────────────┤
  │  [ daily index line chart, full width ]    │
  ├────────────────────────────────────────────┤
  │  Routes            │  Lead-time curve      │
  │  (ranked table)    │  (T+1 … T+45)         │
  ├────────────────────────────────────────────┤
  │  Against official CPI (General Index)      │
  ├────────────────────────────────────────────┤
  │  Ask about the index  [chat]               │
  └────────────────────────────────────────────┘

## Principles
1. The index value is the one bold element. Everything else is quiet.
2. Provenance is visible, not buried: basket version, % imputed, and the fact
   that APW weights are assumptions appear on the page, not in a tooltip.
3. No motion except what answers a click. No card hover lifts.
4. Every number that rests on an assumption is marked in --provisional.
