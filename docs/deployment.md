# Setup and deployment

Order matters. Supabase first (everything writes to it), then the collector
(so data starts accumulating today), then the Space, then Vercel.

Total: about an hour. The collector step is the one with a deadline attached —
your 30-day back-test needs 30 days of calendar time.

---

## 0. Local check first (5 min, no credentials)

```bash
unzip apix.zip && cd apix
pip install -r requirements.txt
pytest -q                            # 43 tests
python -m scripts.collect --dry-run  # full pipeline, no network, no keys
```

If those pass, the code is sound and everything below is configuration.

---

## 1. Supabase

1. New project at supabase.com. Note the database password.
2. SQL Editor → paste all of `sql/schema.sql` → Run.
3. Verify: Table Editor should show `fare_quotes`, `elementary_cells`,
   `apix_daily`, `apix_route_daily`, `apix_monthly`, `collection_runs`,
   `basket_version`, `route_weights`, `chat_sessions`, `chat_messages`.
4. Settings → API. Copy three things:
   - Project URL → `SUPABASE_URL`
   - `anon` `public` key → `NEXT_PUBLIC_SUPABASE_ANON_KEY` (Vercel only)
   - `service_role` key → `SUPABASE_SERVICE_KEY` (GitHub + Space only)

**The service_role key bypasses row-level security.** It goes in GitHub Actions
secrets and Hugging Face Space secrets. It never goes in Vercel, never in a
`NEXT_PUBLIC_` variable, never in a commit. See `docs/secrets.md`.

Sanity check that RLS works: in the SQL Editor run
`select count(*) from fare_quotes;` — it succeeds as service role. The anon key
has no policy on that table at all, so the browser cannot read it.

---

## 2. Collector on GitHub Actions — do this today

1. Push the repo to GitHub.
2. Settings → Secrets and variables → Actions → New repository secret:

   | Name | Value |
   |---|---|
   | `SUPABASE_URL` | from step 1 |
   | `SUPABASE_SERVICE_KEY` | service_role key |
   | `TRAVELPAYOUTS_TOKEN` | travelpayouts.com -> Tools -> API |
   | `AGGREGATOR_API_TOKEN` | Duffel, only if you move to a live token |
   | `ADMIN_TOKEN` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
   | `SPACE_URL` | fill in after step 3 |

3. `config/basket.yaml` ships with `ota:travelpayouts` enabled and everything
   else off. That is the intended configuration — see the note below on why
   Duffel is not the starting source.
4. Actions tab → **Collect airfares** → Run workflow. Watch it once manually
   before trusting the schedule.

After that it runs daily at 01:30 UTC (07:00 IST). Four things about GitHub's
scheduler that are already handled in the workflow but worth knowing:

- Cron is UTC with no timezone option, and runs queue — often 5-30 minutes
  late, occasionally dropped. `quote_ts` records real observation time.
- **Schedules are disabled after 60 days without commit activity.** Workflow
  runs do not count. `keepalive.yml` pushes a weekly heartbeat commit for
  exactly this reason.
- A missed day is permanent. No source will tell you yesterday's fare.
- Overlapping runs would double request load on sources; `concurrency` blocks it.

Verify it worked: `select count(*), max(quote_date) from elementary_cells;`

---

## 3. API on Hugging Face Spaces

**Before you push:** Spaces reads its Docker configuration from YAML
frontmatter at the top of the repo-root `README.md`. Our `README.md` is the
project readme and has none, so the Space will build and then fail to serve.
`SPACE_README.md` holds the correct frontmatter — rename it to `README.md` in
the Space repo.

1. New Space → SDK **Docker** → hardware **CPU basic (free)**.
2. Push the repo, with `SPACE_README.md` renamed to `README.md`:

   ```bash
   git clone https://huggingface.co/spaces/<user>/apix hf-space
   rsync -a --exclude .git --exclude web --exclude node_modules ./ hf-space/
   cd hf-space
   mv SPACE_README.md README.md
   git add -A && git commit -m "deploy apix api" && git push
   ```

   `.dockerignore` keeps `web/` and the 10 MB of source `.xlsx` workbooks out
   of the image; the parsed CSVs in `data/` are what the API actually reads.
3. Settings → Variables and secrets:

   | Name | Value |
   |---|---|
   | `GROQ_API_KEY` | from console.groq.com |
   | `SUPABASE_URL` | from step 1 |
   | `SUPABASE_SERVICE_KEY` | service_role key |
   | `ADMIN_TOKEN` | same value as GitHub |
   | `ALLOWED_ORIGINS` | your Vercel URL once you have it, not `*` |

4. Wait for the build. Check `https://<user>-<space>.hf.space/health`.
5. Put that base URL into the GitHub secret `SPACE_URL`.

Two cpu-basic constraints the design already works around: the disk is
ephemeral (Supabase is the source of truth, parquet in the container is only a
cache) and the Space sleeps when idle (first request takes ~30s to wake, which
is why the chat proxy allows 60s).

---

## 4. Dashboard on Vercel

```bash
cd web
npm install
npm run dev        # http://localhost:3000
```

Then: vercel.com → New Project → import the repo → **Root Directory: `web`**.

Environment variables:

| Name | Value |
|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | Project URL |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | **anon** key |
| `APIX_API_URL` | `https://<user>-<space>.hf.space` |

`APIX_API_URL` has no `NEXT_PUBLIC_` prefix on purpose — it is read server-side
in `app/api/chat/route.ts`, so the browser never learns where the API lives.

Deploy, then go back to the Space and set `ALLOWED_ORIGINS` to your Vercel
domain.

The page degrades honestly: no Supabase config shows setup instructions, no
published index shows the commands to run. Neither is a blank screen.

---

## 4a. Which fare source to run

`config/basket.yaml` enables **Travelpayouts** and nothing else. Reasoning:

| Source | Free | Real Indian fares | Fit |
|---|---|---|---|
| Duffel **test** | yes | no — synthetic carrier ZZ | plumbing checks only |
| Duffel **live** | ~$23/mo | yes, if Indian carriers are covered | production |
| Travelpayouts | yes | yes, but cached up to 7 days | prototype |
| Amadeus Self-Service | — | — | **shut down 17 July 2026** |
| Kiwi Tequila | — | — | invite-only, no self-serve signup |

Travelpayouts is the only free source returning genuine Indian domestic fares
today, which is why it starts the 30-day clock. Its limits are real and belong
in your methodology note rather than hidden: prices come from a cache of
Aviasales search history, only routes people actually searched are present, and
the endpoint returns cheapest fares rather than the full fare ladder — so
within-cell dispersion is understated.

The adapter compensates where it can. It reads `expires_at`, estimates cache
age, and files a two-day-old quote at T+9 rather than T+7, because the person
who saw that price was booking two days further out. Anything older than
`max_cache_age_days` is dropped, and `stale_share()` reports what fraction that
was.

For production the answer is Duffel live or direct carrier feeds — check
Duffel's Airlines page for 6E, AI, QP and SG first, because if they are absent
no aggregator helps and the right answer is a feed obtained under the
Collection of Statistics Act, 2008. See `docs/legal-basis.md`.

## 5. Data still to fetch by hand

Nothing below blocks deployment.

- **CPI air-fare item weight** — `mospi.gov.in/percentage-share` (DataViz — CPI
  Weights). Save as `data/cpi_2024_weights.csv` per the template. Enables
  `/v1/cpi-impact`, which returns 503 with a pointer until then rather than
  inventing a number.
- **DGCA monthly average fare** — file the RTI now; it takes 30 days. See
  `docs/data-sources.md` for why this series may not exist publicly and what
  to use instead.
- **ATF prices** — data.gov.in, for the fuel-cost correlation.

Already ingested: DGCA city-pair traffic, route weights, event calendar, and
the MoSPI CPI General Index (`data/cpi_general_index.csv`, Jan 2025 – Jul 2026,
38 states).

---

## Troubleshooting

**Collector: "no sources enabled"** — `enabled: true` is not set on
`gds:aggregator`, or the replay archive path does not exist.

**Collector runs, no rows** — check `TRAVELPAYOUTS_TOKEN` is set. The
compliance gate logs a warning and returns empty rather than crashing, so read
the Actions log, not the exit code.

**Every carrier comes back as `ZZ`** — you are on a Duffel *test* token. Test
mode serves a synthetic airline whose prices Duffel states are not realistic.
Those quotes are dropped by default; set `APIX_ALLOW_SANDBOX=1` only when
deliberately testing the plumbing.

**Some basket routes return nothing from Travelpayouts** — expected, and worth
recording. The feed is a cache of Aviasales user searches, so thin routes have
thin coverage. List the empty routes in your methodology note; it is a real
finding about the source, not a bug.

**Space builds, `/health` 503s** — no index tables yet. Run
`python -m scripts.rebuild` or wait for the first collection.

**Dashboard blank** — check the browser console for a Supabase RLS error. If
`apix_daily` returns nothing with a valid anon key, the `select` policies in
`schema.sql` did not apply; re-run that section.

**Chat returns "index service is waking up"** — cold Space. Ask again.
