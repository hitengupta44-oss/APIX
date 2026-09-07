# Secrets

No `.env` files. Nothing in this codebase reads a dotfile — `python-dotenv` is
not a dependency, and every credential is read from `os.environ` at the point
of use. Each deployment surface holds its own secrets.

## Where each key lives

### GitHub → Settings → Secrets and variables → Actions

The workflows reference these names exactly; do not rename them.

| Name | Value | Used by |
|---|---|---|
| `TRAVELPAYOUTS_TOKEN` | Travelpayouts affiliate token | `collect.yml` |
| `AGGREGATOR_API_TOKEN` | Duffel token, once you go live | `collect.yml` |
| `SUPABASE_URL` | `https://<ref>.supabase.co` | `collect.yml`, `keepalive.yml` |
| `SUPABASE_SERVICE_KEY` | service role key | `collect.yml`, `keepalive.yml` |
| `SPACE_URL` | `https://<user>-<space>.hf.space` | `collect.yml` |
| `ADMIN_TOKEN` | any long random string you generate | `collect.yml` |

`ADMIN_TOKEN` is yours to invent — it only guards `/admin/reload`. Generate it
with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and paste
the same value into both GitHub and the Space.

### Hugging Face Space → Settings → Variables and secrets

`GROQ_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `ADMIN_TOKEN`,
`ALLOWED_ORIGINS` (set this to your Vercel domain, not `*`, once the frontend
is deployed).

### Vercel → Project → Settings → Environment Variables

`NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_ANON_KEY`. **Nothing else.**

## The one that will hurt you

**The Supabase service role key must never reach Vercel or any browser.** It
bypasses row-level security completely — anyone who opens devtools on your
dashboard would have full read and write access to every table.

The `NEXT_PUBLIC_` prefix in Next.js means "ship this to the client". Putting
the service key behind that prefix is the single most common way a student
project leaks its database, and it looks like it works right up until it
doesn't.

`sql/schema.sql` is written on the assumption that only the anon key reaches
the browser: anon can read the published index tables and has **no policy at
all** on `fare_quotes`, so raw fare data is unreachable from the client. That
separation is legal as much as technical — you publish a derived statistic, you
do not redistribute anyone's fare data.

If the frontend ever needs something privileged, proxy it through a Next.js
route handler where the key stays server-side.

## Local development

Set nothing. Both of these run with zero credentials:

```bash
pytest -q                              # 43 tests
python -m scripts.collect --dry-run    # full pipeline, no network, no keys
```

`--dry-run` exercises config loading, source construction, the compliance gate,
cleaning and store serialisation. It is how you should develop by default.

If you genuinely need to hit live Supabase from your machine, export in the
shell session so the value dies when you close the terminal:

```bash
export SUPABASE_URL="https://xxxx.supabase.co"
export SUPABASE_SERVICE_KEY="eyJ..."
python -m scripts.rebuild
```

On Windows Command Prompt, `set NAME=value` with no quotes and no spaces
around the `=`. In PowerShell, `$env:NAME="value"`.

Do not put these in `.bashrc`, `.zshrc`, or Windows `setx`. All three write the
value to disk permanently, which is the thing you were avoiding. Use the
session-scoped form so the value dies with the terminal window.

There is deliberately no `.env` file and no `python-dotenv` dependency. If you
find yourself wanting one, that is the moment to re-read this page.

## Guardrails in the repo

`.gitignore` blocks `.env*`, `*.key`, `*.pem`, service-account JSON, and
`data/cpi_2024_weights.csv`.

`scripts/check_secrets.py` runs as the first CI step on every push and fails
the build on Duffel tokens, Groq keys, Supabase JWTs and project URLs, AWS
access keys, and generic `api_key = "..."` assignments. Run it yourself any
time:

```bash
python scripts/check_secrets.py
```

## If a key does get committed

Rotate it. Deleting the line does not remove it from git history, and a public
repo is scraped by bots within minutes of a push. Revoke and reissue in the
provider console first, then worry about cleaning history.
