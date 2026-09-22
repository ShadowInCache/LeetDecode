# LeetCode → English Translator — SRS / Build Plan (MVP)

## 1. Project Summary

A Chrome extension that lets a user paste a LeetCode problem's description into a small popup UI. The extension sends this text to a FastAPI backend, which checks a cache and, on a miss, calls an LLM to translate the problem into plain, simple English with 1–2 worked examples. Result is displayed back in the popup.

**Explicitly out of scope for MVP:**
- No DOM scraping / auto-detection of the LeetCode page (user pastes manually)
- No "Quick Check" / quiz feature
- No hints or optimal-solution feature
- No user accounts / login (anonymous usage tracked by extension install ID)

**Notable side-benefit of the paste-based approach:** the extension never needs `host_permissions` for `leetcode.com`. It's a self-contained popup that only talks to your own API. This means less permission friction during Chrome Web Store review and more user trust (nothing is "reading" LeetCode's pages).

---

## 2. Goals

| Goal | Success Metric |
|---|---|
| Reduce confusion reading problem statements | User gets a plain-English restatement + examples in under 5 seconds (cache hit) |
| Control LLM cost | >80% of requests served from cache after first 2 weeks |
| Ship a working MVP fast | End-to-end flow working in ~7 days |

---

## 3. User Flow (MVP)

1. User installs extension, clicks the extension icon.
2. Popup opens — a small window with a textarea: "Paste your LeetCode problem description here."
3. User pastes the full problem text (title + description + examples + constraints) and clicks **"Simplify Problem."**
4. Extension sends the text to the backend along with its anonymous install ID.
5. Backend:
   - Normalizes the text and computes a hash; also extracts a likely title from the first line for fallback matching.
   - Checks the `problems_cache` table for an existing translation (exact hash match, then fallback title match).
   - **Cache hit** → returns stored JSON immediately, no quota deducted.
   - **Cache miss** → checks the user's remaining free quota:
     - Quota available → calls the LLM, validates + stores the result, returns it, increments usage.
     - Quota exhausted → returns `403 QUOTA_EXCEEDED`.
6. Popup renders the response in a fixed format:
   ```
   WHAT YOU NEED TO DO
   ...

   INPUT
   ...

   OUTPUT
   ...

   IMPORTANT
   - ...

   EXAMPLE
   ...
   ```
7. If quota is exhausted, popup shows an upgrade/limit-reached message instead of an empty state.

---

## 4. Free Tier / Rate Limiting Logic

- Every install gets a unique **anonymous install ID**, generated once via `crypto.randomUUID()` and stored in `chrome.storage.local`.
- Backend maintains a **pre-cached set** of ~200–250 most-asked problems + the current LeetCode Daily Problem. Requests matching this set are **always free and unlimited** — pure cache read, no LLM call, no quota deduction.
- Requests for problems **not** in the pre-cached set count against the user's **5 free LLM-generated translations**, tracked server-side in `usage_log` keyed by install ID.
- **Soft abuse guard:** also log the requester's IP alongside install ID. If a single IP is associated with an unusual number of distinct install IDs in a short window, flag it (log only for MVP — don't hard-block, since shared/office IPs are common; revisit if abuse actually shows up in the data).
- On quota exhaustion, return a specific error code (`QUOTA_EXCEEDED`) so the extension can show a clear message. No payment integration in MVP — just messaging.

---

## 5. Tech Stack

### Frontend (Extension)
- **Manifest V3**, no `host_permissions` needed (paste-based, not scraping)
- Plain HTML/CSS/JS popup — no React/build step needed for something this small; keeps the extension trivial to review and ship
- `chrome.storage.local` for install ID + a locally cached copy of remaining quota (reconciled against the server's response each call, server is source of truth)

### Backend
- **FastAPI** (Python), **Pydantic** models for strict request/response validation (critical for parsing LLM output reliably)
- **SQLModel** (or plain SQLAlchemy) as the ORM layer over Postgres
- **Postgres** — single source of truth for cache + usage tracking (no Redis needed at this scale; add it later only if lookup latency actually becomes a measured problem)
- **APScheduler** (in-process) — handles the daily-problem pre-cache job on a schedule, avoiding the need for separate cron infrastructure
- **LLM API** — Claude Haiku-class model (cheapest tier suited for a simplification/rewriting task rather than deep reasoning)

### Deployment
- Backend: **Railway** (simple git-push deploy, includes a Postgres add-on so DB + API can live in one project)
- Database: Railway Postgres, or **Supabase** if you want a free tier with a dashboard for eyeballing the cache table while debugging
- Extension: Chrome Web Store (unpacked/dev mode during testing, submit once stable)

---

## 6. System Architecture

```
[Chrome Extension Popup]  (no host_permissions needed)
        |
        | POST /translate  { install_id, raw_text }
        v
[FastAPI Backend]
        |
        |-- 1. Normalize + hash raw_text; extract likely title (first line)
        |-- 2. Look up problems_cache (hash match, then title fallback)
        |       |-- HIT  -> return cached JSON (no quota touched)
        |       |-- MISS -> check usage_log quota
        |                     |-- quota OK -> call LLM -> validate (Pydantic)
        |                     |             -> store in cache -> return JSON
        |                     |-- quota exceeded -> 403 QUOTA_EXCEEDED
        |
        |-- [APScheduler background job, once/24h]
        |       -> fetch LeetCode daily problem -> pre-generate -> cache it
        v
[Postgres: problems_cache, usage_log]
[LLM API (Claude Haiku)]
```

---

## 7. Data Models

### `problems_cache`
| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| problem_hash | TEXT (unique, indexed) | SHA-256 of normalized input text |
| problem_title | TEXT (indexed) | Extracted first line, used for fallback fuzzy match |
| simplified_json | JSONB | The structured LLM output |
| is_preseeded | BOOLEAN | True for the 200–250 curated problems + daily problem |
| created_at | TIMESTAMP | |
| last_used_at | TIMESTAMP | For future LRU/analytics use |

### `usage_log`
| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| install_id | TEXT (indexed) | Anonymous extension ID |
| ip_address | TEXT | For soft abuse-pattern logging only |
| free_llm_calls_used | INT | Increments only on cache-miss requests |
| last_request_at | TIMESTAMP | |

### Simplified Output JSON Schema (LLM response contract)
```json
{
  "what_you_need_to_do": "string",
  "input": "string",
  "output": "string",
  "important_notes": ["string", "string"],
  "example": {
    "input": "string",
    "output": "string",
    "explanation": "string"
  }
}
```
The LLM is prompted to return **only** this JSON — no markdown fences, no preamble — so the backend can parse and validate it against a Pydantic model before ever writing it to cache. A response that fails validation is never cached and triggers a clear error instead of a silently broken result.

---

## 8. API Endpoints

### `POST /translate`
**Request:**
```json
{ "install_id": "uuid-string", "raw_text": "full pasted problem text" }
```
**Response (200):**
```json
{ "source": "cache" | "llm", "data": { ...simplified output schema... } }
```
**Response (403 — quota exceeded):**
```json
{ "error": "QUOTA_EXCEEDED", "message": "You've used your 5 free translations. Pre-cached common problems remain free." }
```

### `GET /usage/{install_id}`
Returns remaining free quota, so the popup can display "3 of 5 free translations remaining" without guessing client-side.

### `GET /health`
Basic health check for deployment monitoring.

---

## 9. Caching Strategy Detail

1. **Pre-seed script** (run once, then re-run when you want to expand the list):
   - Compile the 200–250 most-asked LeetCode problems from public "top interview questions" lists.
   - Run each through the LLM once, store with `is_preseeded = true`.
2. **Daily problem job** (APScheduler, runs every 24h):
   - Fetch LeetCode's current daily challenge via their public GraphQL endpoint.
   - Pre-generate and cache its translation, `is_preseeded = true`.
3. **Cache matching**, since users paste free-form text rather than send a clean slug:
   - Primary: hash of normalized (trimmed, lowercased) full text → exact match.
   - Fallback: extracted title (first line) matched against known pre-seeded titles, so minor paste variations (extra blank lines, copied constraints in a different order) still hit cache.
4. **Eviction**: not needed at MVP scale — JSON text storage in Postgres is cheap even at a few thousand rows.

---

## 10. Non-Functional Requirements

- **Latency**: cache hits <500ms; LLM calls acceptable up to ~5s.
- **Cost control**: hard per-install quota enforced server-side; log every LLM call (tokens + cost estimate) for auditing.
- **Privacy**: only anonymous install ID + pasted problem text is collected. No LeetCode account data. Advise users in the popup not to paste personal solution code, only the problem statement.
- **Reliability**: a failed or schema-invalid LLM response is never cached and always surfaces a clear error to the user rather than a broken partial result.

---

## 11. Build Plan (Day-by-Day)

| Day | Task |
|---|---|
| **Day 1** | Scaffold Chrome extension (Manifest V3, no host_permissions), popup UI with textarea + "Simplify Problem" button; scaffold FastAPI project skeleton in parallel |
| **Day 2** | Wire up `/translate` endpoint calling the LLM directly (no cache yet), Pydantic validation of the JSON contract; connect extension to backend — get one full end-to-end round trip working |
| **Day 3** | Add Postgres + `problems_cache` table; implement hash-based cache lookup before the LLM call; confirm cache hit/miss both work correctly |
| **Day 4** | Add install ID generation in the extension + `usage_log` table + quota enforcement (5 free calls), `/usage/{install_id}` endpoint, popup shows remaining quota |
| **Day 5** | Pre-seed script: compile the 200–250 common problems list, batch-generate and cache translations |
| **Day 6** | APScheduler daily-problem job (fetch + pre-cache LeetCode's daily challenge); add title-fallback matching for near-duplicate pastes |
| **Day 7** | Polish popup UI and error states (quota exceeded, LLM failure), deploy backend to Railway, package extension for Chrome Web Store submission |

---

## 12. Open Questions / Decisions for Later (Post-MVP)
- Payment integration once free tier is exhausted (Stripe, etc.)
- Auto-detecting the LeetCode page instead of manual paste (would require a content script + `host_permissions` — a deliberate trade-off away from the MVP's zero-permission simplicity)
- Hint system / optimal solution feature (from original vision, deferred)
- Harder abuse prevention if install-ID-based quota bypass turns out to be a real problem in practice
- Adding Redis only if measured DB latency under real load actually justifies it
