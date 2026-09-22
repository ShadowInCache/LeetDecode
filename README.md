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
│   1. hash(raw_text) → problems_cache            │  hit  → free, no quota
│   2. title fallback → preseeded rows            │  hit  → free, no quota
│   3. quota check (5 per install)                │  over → 403 QUOTA_EXCEEDED
│   4. Gemini → validate → (on failure) Grok      │  fail → 502, no quota spent
│   5. cache + increment                          │
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
| `XAI_API_KEY` | no¹ | — | [console.x.ai](https://console.x.ai/) — needed only if Grok is in the chain |
| `DATABASE_URL` | yes | — | Railway injects this automatically |
| `LLM_PROVIDER` | no | `gemini` | `gemini` \| `grok` |
| `LLM_FALLBACK_PROVIDER` | no | `grok` | Blank disables failover |
| `GEMINI_MODEL` | no | `gemini-3.1-flash-lite` | $0.25/$1.50 per 1M tokens |
| `GROK_MODEL` | no | `grok-4.3` | $1.25/$2.50 per 1M tokens |
| `LLM_MAX_TOKENS` | no | `2000` | Per-translation output cap |
| `FREE_CALL_LIMIT` | no | `5` | Free generations per install |
| `CORS_ALLOW_ORIGINS` | no | `*` | Comma-separated, or `*` |
| `ENABLE_SCHEDULER` | no | `true` | `false` to skip the daily job |

¹ You need a key for whichever providers are in the chain. With the defaults,
Gemini is required and Grok is optional — a missing `XAI_API_KEY` just means the
fallback is skipped rather than an error.

---

## API

| Endpoint | Purpose |
|---|---|
| `POST /translate` | `{install_id, raw_text}` → `{source: "cache"\|"llm", data: {...}}` |
| `GET /usage/{install_id}` | `{free_calls_used, free_calls_remaining}` |
| `GET /health` | `{status: "ok"}` |

`POST /translate` responses:

| Status | Body | When |
|---|---|---|
| 200 | `{source, data}` | Served from cache or freshly generated |
| 403 | `{detail: {error: "QUOTA_EXCEEDED", message}}` | Cache miss, 5 free calls already used |
| 422 | FastAPI validation detail | `raw_text` under 20 chars, over 20,000, or a missing field |
| 502 | `{detail: {error: "TRANSLATION_FAILED", message}}` | Every provider failed — **no quota spent** |

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
pytest -q          # 101 tests, no API key or database server required
```

The suite runs against in-memory SQLite with the LLM providers faked, so it needs
no credentials and no running Postgres. It covers schema rejection, provider
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
4. **Set the variables** — at minimum `GEMINI_API_KEY`. Add `XAI_API_KEY` if you
   want the Grok fallback live.
5. **Deploy.** [`railway.json`](backend/railway.json) supplies the start command
   and points the healthcheck at `/health`; [`Procfile`](backend/Procfile) is
   there as a fallback for other Nixpacks/Heroku-style platforms.
6. **Seed the cache** — from the Railway shell, or locally with `DATABASE_URL`
   pointed at the production database:
   ```
   python scripts/preseed.py
   ```
7. **Point the extension at it** — set `BACKEND_BASE_URL` in
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
    models.py          SQLModel tables: problems_cache, usage_log
    db.py              Engine, session dependency, table creation
    cache.py           Normalise, hash, title extraction, lookup, store
    usage.py           Per-install quota accounting
    daily.py           LeetCode GraphQL fetch + HTML→text + cache
    scheduler.py       APScheduler wiring for the 24h job
    llm/
      prompt.py        The one shared system prompt
      base.py          LLMProvider protocol + typed errors
      gemini.py        Google Gemini adapter (primary)
      grok.py          xAI Grok adapter (fallback)
      service.py       Provider chain, failover, validation gate
    routers/           health.py, translate.py, usage.py
  scripts/             preseed.py, run_daily_job.py, fetch_daily_only.py
  data/                seed_problems.json
  tests/               101 tests
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

**Postgres only.** No Redis. Two append-mostly tables at this scale do not
justify a second datastore; revisit if measured lookup latency ever says
otherwise.

---

## Not built, deliberately

Out of scope for this MVP: DOM scraping or auto-detection of the LeetCode page,
quizzes or quick-checks, hints, optimal-solution output, user accounts, and
payments. Quota exhaustion shows a message, not a checkout.
