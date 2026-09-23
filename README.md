# LeetDecode

Paste a LeetCode problem into a Chrome popup, get it back in plain English with a
worked example.

LeetDecode is a Manifest V3 Chrome extension plus a FastAPI backend. The backend
caches every translation in Postgres, so the same problem is only ever sent to an
LLM once. A curated set of common problems plus the daily challenge are
pre-cached and free for everyone, forever; anything outside that set costs one of
five free generations per install.

```
┌─────────────────────┐
│  Chrome popup (MV3) │  no host_permissions, no content scripts
│  paste → Simplify   │
└──────────┬──────────┘
           │  POST /translate { install_id, raw_text }
           ▼
┌─────────────────────────────────────────────────┐
│  FastAPI                                        │
│   1. per-IP request limit                       │  over → 429 + Retry-After
│   2. hash / title → problems_cache              │  hit  → free, uncounted
│   3. per-IP cache-miss limit                    │  over → 429  (cost brake)
│   4. new-install-per-IP limit                   │  over → 429  (bypass guard)
│   5. global daily LLM cap                       │  over → 503, cache-only
│   6. per-install quota (5)                      │  over → 403 QUOTA_EXCEEDED
│   7. Gemini → validate → (on failure) Groq      │  fail → 502, nothing spent
│   8. cache + commit the reservations            │
│                                                 │
│  APScheduler, every 24h: LeetCode daily problem │
└──────────┬──────────────────────────────────────┘
           ▼
     Postgres  (problems_cache, usage_log)
```

---

## Quickstart

### 1. Backend

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt        # includes requirements.txt

copy .env.example .env                     # then fill in the values below
uvicorn app.main:app --reload --port 8000
```

Check it:
```powershell
curl http://127.0.0.1:8000/health          # {"status":"ok"}
```
Interactive API docs: <http://127.0.0.1:8000/docs>

You need at minimum a `GEMINI_API_KEY` and a `DATABASE_URL` in `.env`.

### 2. Database

Any Postgres 13+ works. Locally:
```powershell
docker run --name leetdecode-db -e POSTGRES_PASSWORD=postgres -p 5432:5432 -d postgres:16
```
```ini
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres
```
Tables are created automatically on first boot — no migration step.

> **No Postgres handy?** `DATABASE_URL=sqlite:///./leetdecode.db` works for local
> poking. `simplified_json` is declared as `JSON` with a Postgres `JSONB`
> variant, so the same models run on both. Use Postgres for anything real.

**Using Supabase instead?** Take the **session pooler** connection string
(*Settings → Database → Connection string → Session pooler*), port 5432 on
`…pooler.supabase.com`. Do **not** use the direct `db.<ref>.supabase.co`
string: it is IPv6-only on the free tier and Railway is IPv4, so it fails with
an unhelpful timeout. The transaction pooler (6543) is also wrong here — it
disallows prepared statements and targets serverless, not a long-lived FastAPI
process. Note the username is `postgres.<project-ref>`, not `postgres`. Free
Supabase projects also pause after about a week of inactivity.

### 3. Extension

1. Open `chrome://extensions`
2. Turn on **Developer mode**
3. **Load unpacked** → select the `extension/` folder
4. Click the LeetDecode icon in the toolbar

If your backend isn't on `http://127.0.0.1:8000`, change `BACKEND_BASE_URL` at
the top of [`extension/popup.js`](extension/popup.js) and hit reload on the
extensions page.

---

## Configuration

All backend settings are environment variables, documented in
[`backend/.env.example`](backend/.env.example).

| Variable | Required | Default | Notes |
|---|---|---|---|
| `GEMINI_API_KEY` | yes¹ | — | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `GROQ_API_KEY` | no¹ | — | [console.groq.com/keys](https://console.groq.com/keys) — needed only if Groq is in the chain |
| `DATABASE_URL` | yes | — | Railway injects it; for Supabase use the **session pooler** string |
| `DB_POOL_SIZE` | no | `5` | Client-side pool depth |
| `DB_MAX_OVERFLOW` | no | `5` | Extra connections above the pool |
| `DB_POOL_RECYCLE_SECONDS` | no | `1800` | Recycle before a pooler drops idle handles |
| `DB_POOL_PRE_PING` | no | `true` | Validate connections; costs ~55ms/request |
| `LLM_PROVIDER` | no | `gemini` | `gemini` \| `groq` |
| `LLM_FALLBACK_PROVIDER` | no | `groq` | Blank disables failover |
| `GEMINI_MODEL` | no | `gemini-3.1-flash-lite` | $0.25/$1.50 per 1M tokens |
| `GROQ_MODEL` | no | `openai/gpt-oss-120b` | $0.15/$0.60 per 1M tokens |
| `LLM_MAX_TOKENS` | no | `2000` | Per-translation output cap |
| `LLM_TIMEOUT_SECONDS` | no | `45` | Per-call ceiling; fail over rather than hang |
| `FREE_CALL_LIMIT` | no | `5` | Free generations per install |
| `RATE_LIMITS_ENABLED` | no | `true` | `false` disables all limiting (local dev only) |
| `RATE_LIMIT_REQUESTS_PER_HOUR` | no | `120` | All requests, per IP |
| `RATE_LIMIT_LLM_PER_HOUR` | no | `20` | Cache misses, per IP — the cost brake |
| `RATE_LIMIT_NEW_INSTALLS_PER_HOUR` | no | `3` | New install_ids per IP — closes quota bypass |
| `LLM_DAILY_CAP` | no | `1000` | Global LLM calls/day; trips cache-only mode |
| `CORS_ALLOW_ORIGINS` | no | `*` | Comma-separated, or `*` |
| `ENABLE_SCHEDULER` | no | `true` | `false` to skip the daily job |
| `DAILY_JOB_HOUR` | no | `3` | UTC hour for the daily job (wall-clock, not an interval) |
| `ADMIN_TOKEN` | no | — | Bearer token for /admin; **empty disables the dashboard** |
| `SENTRY_DSN` | no | — | Empty disables error tracking entirely |
| `SENTRY_ENVIRONMENT` | no | `development` | Tags events; non-`development` switches logs to JSON |
| `SENTRY_TRACES_SAMPLE_RATE` | no | `0.1` | Fraction of requests traced |
| `SENTRY_SEND_PII` | no | `false` | **Off by default** — see Privacy below |

¹ You need a key for whichever providers are in the chain. With the defaults,
Gemini is required and Groq is optional — a missing `GROQ_API_KEY` just means the
fallback is skipped rather than an error.

---

## API

| Endpoint | Purpose |
|---|---|
| `POST /translate` | `{install_id, raw_text}` → `{source: "cache"\|"llm", data: {...}}` |
| `GET /usage/{install_id}` | `{free_calls_used, free_calls_remaining}` |
| `GET /health` | `{status: "ok"}` |
| `GET /admin` | Usage + cost dashboard (needs `ADMIN_TOKEN`) |
| `GET /admin/stats` | The same data as JSON |

`POST /translate` responses:

| Status | Body | When |
|---|---|---|
| 200 | `{source, data}` | Served from cache or freshly generated |
| 403 | `{detail: {error: "QUOTA_EXCEEDED", message}}` | Cache miss, 5 free calls already used |
| 422 | FastAPI validation detail | `raw_text` under 20 chars, over 20,000, or a missing field |
| 429 | `{detail: {error: "RATE_LIMITED", message}}` + `Retry-After` | A per-IP limit tripped |
| 502 | `{detail: {error: "TRANSLATION_FAILED", message}}` | Every provider failed — **no quota spent** |
| 503 | `{detail: {error: "AT_CAPACITY", message}}` + `Retry-After` | Global daily cap reached; cache still served |

The translation payload is always exactly this shape, validated before it is
cached or returned:

```json
{
  "what_you_need_to_do": "string",
  "input": "string",
  "output": "string",
  "important_notes": ["string", "..."],
  "example": { "input": "string", "output": "string", "explanation": "string" }
}
```

---

## Scripts

All run from `backend/` with the venv active.

```powershell
# Batch-translate the curated problem list into the cache.
python scripts/preseed.py                  # uses data/seed_problems.json
python scripts/preseed.py --dry-run        # parse and report, no LLM calls
python scripts/preseed.py --limit 5        # trial run
python scripts/preseed.py --file mine.csv  # JSON or CSV
python scripts/preseed.py --delay 1.0      # pace the provider

# Fetch + cache today's LeetCode daily problem right now.
python scripts/run_daily_job.py

# Fetch the daily problem and print it. No LLM, no API key, no cost.
python scripts/fetch_daily_only.py

# One real call per configured provider, validated. Costs a fraction of a cent.
python scripts/smoke_llm.py
python scripts/smoke_llm.py --show

# Send one test error to Sentry and confirm it arrives.
python scripts/verify_sentry.py

# Check this environment is configured correctly. No LLM calls, no cost.
python scripts/preflight.py
railway run python scripts/preflight.py   # against the deployed environment
```

`preseed.py` is safe to re-run: anything already cached is skipped without an
LLM call, so an interrupted run resumes where it stopped.

**Expanding the seed list.** [`backend/data/seed_problems.json`](backend/data/seed_problems.json)
ships 15 well-known problems as a starting point; the target is 200–250. Add
entries as `{"title": "...", "body": "..."}` — the script joins them as
`title\n\nbody` so the title lands on the first line, which is what the cache's
title fallback reads. A CSV with `title,body` columns works too.

---

## Tests

```powershell
cd backend
pytest -q          # 183 tests, no API key or database server required
```

The suite runs against in-memory SQLite with the LLM providers faked, so it needs
no credentials and no running Postgres. For a real call against the live
providers, use `scripts/smoke_llm.py` instead. It covers schema rejection, provider
failover, cache hit/miss, quota accounting, the daily job, and seed-file parsing.

---

## Deploying to Railway

1. **Create the project** — in Railway, *New Project → Deploy from GitHub repo*,
   pointed at this repo.
2. **Set the root directory** to `backend` (Settings → Root Directory). The repo
   holds both halves; only `backend/` is deployed.
3. **Add Postgres** — *New → Database → Add PostgreSQL*. Railway injects
   `DATABASE_URL` into the service automatically. (It hands out a `postgres://`
   URL; [`app/db.py`](backend/app/db.py) rewrites that to `postgresql://`, which
   is what SQLAlchemy needs.)
4. **Set the variables** — at minimum `GEMINI_API_KEY`. Add `GROQ_API_KEY` if you
   want the Groq fallback live.
5. **Deploy.** The build uses [`backend/Dockerfile`](backend/Dockerfile), which
   Railway picks up automatically from the root directory. That is deliberate:
   Railway's builder auto-detection (Railpack) failed to recognise this as a
   Python project, and an explicit Dockerfile builds identically everywhere.
   [`railway.json`](railway.json) at the **repo root** sets the healthcheck —
   note that Railway's config file does *not* follow the Root Directory
   setting, so it must live at the root even though the service builds from
   `backend/`. [`Procfile`](backend/Procfile) remains for Heroku-style hosts.
6. **Verify the configuration** — Railway's *Shared Variables* are **not**
   automatically visible to a service; each one must be shared into it (the
   **Share** button, or the service's Variables tab). This is the most common
   way a deploy looks healthy but fails on every translation. Confirm with:
   ```
   railway run python scripts/preflight.py
   ```
7. **Seed the cache** — from the Railway shell, or locally with `DATABASE_URL`
   pointed at the production database:
   ```
   python scripts/preseed.py
   ```
8. **Point the extension at it** — set `BACKEND_BASE_URL` in
   [`extension/popup.js`](extension/popup.js) to your Railway URL, reload the
   unpacked extension.

**If you scale past one instance,** set `ENABLE_SCHEDULER=false` on all but one.
Every instance otherwise runs its own copy of the daily job. That is safe — the
job checks the cache before generating, so duplicates cost a query rather than a
generation — but there is no reason to pay for the extra requests.

---

## Project structure

```
backend/
  app/
    main.py            FastAPI app, CORS, lifespan (tables + scheduler)
    config.py          Env-var settings, provider selection
    schemas.py         Pydantic contracts + the provider-facing JSON schema
    models.py          SQLModel tables: problems_cache, usage_log,
                         rate_limit_bucket, llm_call_log, rate_limit_bucket
    db.py              Engine, session dependency, table creation
    cache.py           Normalise, hash, title extraction, lookup, store
    usage.py           Per-install quota accounting (atomic reserve/refund)
    ratelimit.py       Postgres fixed-window limits + global spend cap
    stats.py           Dashboard aggregates
    preflight.py       Startup + on-demand configuration checks
    bookkeeping.py     Deferred writes, run after the response is sent
    observability.py   Sentry init, PII scrubbing, JSON log formatter
    call_log.py        Per-call cost recording
    pricing.py         Per-model token prices
    daily.py           LeetCode GraphQL fetch + HTML→text + cache
    scheduler.py       APScheduler wiring for the 24h job
    llm/
      prompt.py        The one shared system prompt
      base.py          LLMProvider protocol + typed errors
      gemini.py        Google Gemini adapter (primary)
      groq_provider.py Groq (GroqCloud) adapter (fallback)
      service.py       Provider chain, failover, validation gate
    routers/           health.py, translate.py, usage.py, admin.py
  scripts/             preseed.py, run_daily_job.py, fetch_daily_only.py
  data/                seed_problems.json
  tests/               183 tests
extension/
  manifest.json        MV3, `storage` permission only
  popup.html/css/js    The entire UI
  icons/
```

---

## Design notes

**No `host_permissions`, no content scripts.** The popup only ever talks to your
own API, so the extension requests nothing but `storage`. That means less
friction in Chrome Web Store review and nothing that can read a LeetCode page.
The backend sends permissive CORS headers, which is what lets the popup's
`chrome-extension://` origin reach it without a host permission.

**One validation gate.** Provider adapters return raw text and nothing else;
[`llm/service.py`](backend/app/llm/service.py) is the only place a response is
checked against `SimplifiedProblem`. `/translate`, the daily job and the pre-seed
script all go through it, so no path can write an unvalidated row. A response
that violates the schema is discarded, never coerced.

**Schema-invalid output is treated as a provider failure.** A model that ignores
the contract is as useless as one that is down, so both fall through to the
fallback provider. That is the main reason the second provider exists.

**Failures never cost the user.** The quota counter increments only after a
validated result is in hand. A provider outage, a timeout or a malformed response
returns 502 with the user's five calls intact.

**Cache hits are free and unlimited**, which is what makes the preseeded set free
forever. The title fallback only matches *preseeded* rows — those titles are
curated, whereas an organically cached row carries whatever first line the
original paster happened to have, which isn't trustworthy enough to serve to
someone else on a title match alone.

**Privacy.** `SENTRY_SEND_PII` is off by default, and pasted problem text is
scrubbed from any error payload that does reach Sentry. Sending user IPs and
their pasted text to a third-party processor is a disclosure decision your
privacy policy has to cover, not something to switch on by default.

**Cost is measured, not guessed.** Every LLM call writes an `llm_call_log` row
with real token counts from the provider and a cost estimate from
`pricing.py`. Measured at ~$0.40 per 1,000 translations on the default models.
A model with no price on file logs `cost_usd = NULL` rather than a made-up
number.

**Postgres only.** No Redis. Two append-mostly tables at this scale do not
justify a second datastore; revisit if measured lookup latency ever says
otherwise.

---

## Not built, deliberately

Out of scope for this MVP: DOM scraping or auto-detection of the LeetCode page,
quizzes or quick-checks, hints, optimal-solution output, user accounts, and
payments. Quota exhaustion shows a message, not a checkout.
