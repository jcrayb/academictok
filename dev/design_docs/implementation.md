# AcademicTok — Implementation Plan

A Reddit-style doomscroll for academic papers. Each "subreddit" is a research
field; each "post" is an LLM-written summary of a paper. Python/Flask backend
(port 8080) + Astro frontend (port 4321).

This plan reflects the following decisions:

- **Firebase auth.** Each user signs in with a **Google** popup and subscribes to
  fields; subscriptions are persisted **server-side, keyed by Firebase UID**.
  The auth workflow is modeled on the existing app at `/home/jcrayb/github/app`.
  Firebase project is **`academic-tok`** (web client config in `./firebase_config`).
- **Hybrid fields.** A seeded list of fields exists, but each ingested paper is
  classified by the local LLM into an existing field *or* a newly created one.
- **SQLite** backs all caching and storage (papers, summaries, fields, users,
  subscriptions).
- **Ollama** (local LLM) does both summarization and field classification.

### Auth pattern reused from `/home/jcrayb/github/app`

- **Backend:** `firebase_admin` initialized from
  `credentials.Certificate(FIREBASE_CRED_PATH)`. A `_verify_token(id_token, uid)`
  helper calls `auth.verify_id_token`, confirms the decoded UID matches the
  claimed `uid`, auto-provisions the user row on first sight, and guards UIDs
  against a `^[A-Za-z0-9_-]{1,128}$` regex (defense in depth). `_auth_uid()`
  returns the verified UID or `None`. Token is accepted either as an
  `Authorization: Bearer <token>` header (GET) or in the JSON body
  (`{idToken, uid}`, POST) — same convention as the existing app.
- **Frontend:** port `lib/firebase.ts` (client SDK init + Google popup provider
  + `signOut`), `stores/auth.ts` (`$user`/`$loading` nanostores +
  `getIdToken()`), and a `lib/api.ts` `apiFetch` wrapper. Client config comes
  from the `academic-tok` project (values in `./firebase_config`).

---

## 1. High-level architecture

```
Semantic Scholar API ──(1 req/s)──► Ingestion worker
                                          │
                                  Ollama (summarize + classify)
                                          │
                                       SQLite  ◄──► Flask API (:8080)
                                                         │ JSON
                                                  Astro frontend (:4321)
                                                         │
                                                   localStorage (subscriptions)
```

Two decoupled halves:

1. **Ingestion pipeline** (offline / background): fetch → summarize → classify →
   persist. Never runs during a page load.
2. **Serving layer** (Flask): reads only from SQLite, returns JSON feeds.

The frontend never talks to Semantic Scholar or Ollama directly.

---

## 2. Repository layout

```
academictok/
├── design_doc.md
├── implementation.md
├── README.md
├── requirements.txt
├── .env.example              # SEMANTIC_SCHOLAR_API_KEY, OLLAMA_*, FIREBASE_CRED_PATH, CORS_ORIGINS
├── authkey.json              # Firebase Admin service-account key (GITIGNORED)
├── config.py                 # config loading from env
├── app.py                    # Flask app factory + firebase_admin init + route registration
├── auth.py                   # firebase token verification (_verify_token / _auth_uid) + UID guard
├── db.py                     # SQLite connection + schema init/migrations
├── models.py                 # lightweight data-access functions
├── seeds.py                  # seed list of fields + their search queries
├── ingest/
│   ├── __init__.py
│   ├── semantic_scholar.py   # API client w/ rate limiting + retry
│   ├── summarizer.py         # Ollama summarize prompt
│   ├── classifier.py         # Ollama field-classification prompt
│   └── pipeline.py           # orchestrates fetch→summarize→classify→store
├── routes/
│   ├── __init__.py
│   ├── auth.py               # /api/verifyUserId (provision user on first login)
│   ├── fields.py             # /api/fields ...
│   ├── subscriptions.py      # /api/subscriptions (GET/POST/DELETE, auth-gated)
│   └── feed.py               # /api/feed ...
├── scripts/
│   └── run_ingest.py         # CLI entrypoint to run/seed ingestion
├── tests/
│   └── ...
└── frontend/                 # Astro app (see §7)
```

---

## 3. Data model (SQLite)

```sql
-- A research field ("subreddit").
CREATE TABLE fields (
    id          INTEGER PRIMARY KEY,
    slug        TEXT UNIQUE NOT NULL,        -- e.g. "stochastic-optimization"
    name        TEXT NOT NULL,               -- "Stochastic Optimization"
    description TEXT,                         -- short LLM/seed description
    query       TEXT,                         -- search query used for ingestion (seeded fields)
    is_seed     INTEGER NOT NULL DEFAULT 0,   -- 1 = curated, 0 = LLM-created
    created_at  TEXT NOT NULL
);

-- A paper (one row per unique Semantic Scholar paper).
CREATE TABLE papers (
    id            INTEGER PRIMARY KEY,
    s2_paper_id   TEXT UNIQUE NOT NULL,       -- Semantic Scholar paperId
    title         TEXT NOT NULL,
    abstract      TEXT,
    authors       TEXT,                       -- JSON array of names
    year          INTEGER,
    venue         TEXT,
    url           TEXT,
    citation_count INTEGER,
    fetched_at    TEXT NOT NULL
);

-- LLM-generated summary post for a paper.
CREATE TABLE summaries (
    id           INTEGER PRIMARY KEY,
    paper_id     INTEGER NOT NULL UNIQUE REFERENCES papers(id),
    title        TEXT NOT NULL,               -- punchy summary title
    body         TEXT NOT NULL,               -- summary text
    model        TEXT NOT NULL,               -- ollama model used
    created_at   TEXT NOT NULL
);

-- Which field(s) a paper belongs to (LLM-assigned). Many-to-one in practice,
-- modeled many-to-many to allow a paper to surface in >1 field if desired.
CREATE TABLE paper_fields (
    paper_id   INTEGER NOT NULL REFERENCES papers(id),
    field_id   INTEGER NOT NULL REFERENCES fields(id),
    PRIMARY KEY (paper_id, field_id)
);

-- Tracks processed queries to avoid redundant API calls.
CREATE TABLE ingest_log (
    id          INTEGER PRIMARY KEY,
    query       TEXT NOT NULL,
    s2_paper_id TEXT,
    status      TEXT NOT NULL,                -- fetched | summarized | classified | error
    detail      TEXT,
    created_at  TEXT NOT NULL
);

-- A user, keyed by Firebase UID. Provisioned on first verified login.
CREATE TABLE users (
    uid         TEXT PRIMARY KEY,             -- Firebase UID (regex-guarded)
    created_at  TEXT NOT NULL,
    last_seen   TEXT
);

-- Per-user field subscriptions (server-side; replaces localStorage).
CREATE TABLE subscriptions (
    uid          TEXT NOT NULL REFERENCES users(uid),
    field_id     INTEGER NOT NULL REFERENCES fields(id),
    created_at   TEXT NOT NULL,
    PRIMARY KEY (uid, field_id)
);
```

Indexes on `paper_fields(field_id)`, `papers(s2_paper_id)`, `summaries(paper_id)`,
`subscriptions(uid)`.

**Subscriptions are persisted server-side**, keyed by Firebase UID. The frontend
no longer relies on `localStorage`; it reads/writes subscriptions through
auth-gated API endpoints (§5) so they follow the user across devices.

---

## 4. Ingestion pipeline

### 4.1 Semantic Scholar client (`ingest/semantic_scholar.py`)
- Wrap the `/graph/v1/paper/search` (and/or `/paper/search/bulk`) endpoints.
- **Hard 1 req/sec rate limit**: a small token-bucket / `time.monotonic()` gate
  shared across the process so we never exceed the documented limit.
- Send the API key via the `x-api-key` header.
- Request only needed fields: `paperId,title,abstract,authors,year,venue,url,citationCount`.
- Retry with backoff on 429/5xx; log failures to `ingest_log`.
- Filter: drop papers with no abstract (can't summarize), optionally floor on
  `citationCount`/`year` to favor "important and relevant" papers.

### 4.2 Summarizer (`ingest/summarizer.py`)
- Call Ollama (`/api/generate` or `/api/chat`) with model from env
  (default `qwen3.5:9b` — configurable).
- Prompt produces structured output: a short **title** and a 2–4 sentence
  **body** aimed at a doomscrolling reader. Request JSON, parse defensively.

### 4.3 Classifier (`ingest/classifier.py`)
- Given the paper (title + abstract + summary) **and the current list of fields**
  (name + description), the LLM returns either:
  - an existing field slug, or
  - a proposal for a new field: `{ "new": true, "name": ..., "description": ... }`.
- New fields are inserted with `is_seed=0`; slug generated from name.
- Guardrails to prevent field sprawl:
  - Pass the full existing-field list each call and instruct "strongly prefer an
    existing field."
  - Normalize/slug-match proposed new names against existing fields before
    creating (case/punctuation-insensitive) to avoid near-duplicates.
  - (Optional, later) a periodic merge step.

### 4.4 Orchestration (`ingest/pipeline.py` + `scripts/run_ingest.py`)
- For each seed field query: fetch N papers → for each new paper: summarize →
  classify → store in one transaction.
- Idempotent: skip papers already in `papers` (by `s2_paper_id`).
- Run manually via `python scripts/run_ingest.py [--field SLUG] [--limit N]`.
- Cron/scheduling is out of scope for v1 (run on demand); easy to add later.

---

## 5. Flask API (port 8080)

App factory in `app.py`, which also runs `firebase_admin.initialize_app(...)`.
`flask-cors` restricted to the origins in `CORS_ORIGINS` (fail-closed if unset,
matching the existing app). Auth-gated routes verify the Firebase token via
`auth.py` (`_auth_uid`) before touching user data.

| Method | Route | Auth | Purpose |
|---|---|---|---|
| GET | `/api/health` | no | liveness check |
| POST | `/api/verifyUserId` | token | verify token + provision `users` row on first login |
| GET | `/api/fields` | no | list all fields (slug, name, description, paper count) |
| GET | `/api/subscriptions` | yes | list the current user's subscribed field slugs |
| POST | `/api/subscriptions` | yes | subscribe to a field `{ slug }` |
| DELETE | `/api/subscriptions/<slug>` | yes | unsubscribe |
| GET | `/api/feed?mode=subscribed&page=N` | yes | **subscribed feed** — summaries from the user's subscribed fields (resolved server-side from `subscriptions`) |
| GET | `/api/feed?mode=mixed&page=N` | optional | **main feed** — subscribed fields + discovery from others (falls back to all-fields sampling when signed out) |
| GET | `/api/fields/<slug>?page=N` | no | feed for a single field |
| GET | `/api/papers/<s2_paper_id>` | no | full detail for one paper/post |

- **Auth handling** mirrors the existing app: GET requests carry
  `Authorization: Bearer <idToken>` (+ `uid` query param); the helper verifies
  and returns the UID or 401. The subscribed feed derives its field set from the
  `subscriptions` table for that UID — no field list is trusted from the client.
- Feed item shape:
  `{ paper_id, s2_paper_id, summary_title, summary_body, paper_title, authors, year, venue, url, field: {slug,name} }`.
- Pagination via `page`/`page_size` (keyset on `papers.id` for stable infinite
  scroll).
- "Mixed" feed: interleave subscribed-field items with a sampling of items from
  unsubscribed fields (simple ratio, e.g. 70/30) so users discover new fields.

---

## 6. Frontend (Astro, port 4321) — `frontend/`

- Scaffold with `npm create astro@latest`, add `nanostores` + `firebase` and a
  small amount of client JS (Astro islands or a thin Preact/vanilla component)
  for infinite scroll.
- **Auth (ported from `/home/jcrayb/github/app`):**
  - `src/lib/firebase.ts` — client SDK init from `PUBLIC_FIREBASE_*` env (the
    `academic-tok` values), Google popup provider, `signInWithGoogle`, `signOut`.
  - `src/stores/auth.ts` — `$user`/`$loading` nanostores fed by a single
    `onAuthStateChanged` listener, plus `getIdToken(forceRefresh)`.
  - `src/lib/api.ts` — `apiFetch` wrapper that attaches the bearer token; typed
    helpers for fields / subscriptions / feed.
  - `login.astro` + a small login component — sign-in buttons; on success call
    `POST /api/verifyUserId` to provision the user, then route to the feed.
- **Pages/components:**
  - `index.astro` — the scroll feed with a toggle: **Subscribed** vs **Discover (mixed)**.
    While `$loading` is true, show a skeleton; signed-out users see Discover +
    a prompt to sign in to subscribe.
  - Feed card component — summary title, body, paper metadata, link to source,
    and a "subscribe to this field" button (calls `POST /api/subscriptions`).
  - `fields.astro` — browse/subscribe to fields (subscription state from the API).
- **Subscriptions:** read/written via the auth-gated API (`/api/subscriptions`),
  not `localStorage`, so they sync across devices.
- **Infinite scroll:** IntersectionObserver fetches the next `page`.
- API base URL from `PUBLIC_API_URL` (empty = same-origin; default dev
  `http://localhost:8080`), matching the existing app's convention.

---

## 7. Configuration & secrets

- **Backend** `.env` (gitignored) + `.env.example`:
  - `SEMANTIC_SCHOLAR_API_KEY`
  - `OLLAMA_HOST` (default `http://localhost:11434`)
  - `OLLAMA_MODEL` (default `qwen3.5:9b`)
  - `DATABASE_PATH` (default `./academictok.db`)
  - `FIREBASE_CRED_PATH` (default `authkey.json`) — Firebase Admin service-account key
  - `CORS_ORIGINS` (e.g. `http://localhost:4321`) — fail-closed if unset
  - `PORT` (default `8080`), `DEBUG` (default `false`)
- **Frontend** `frontend/.env` + `frontend/.env.example` — `PUBLIC_FIREBASE_*`
  client config (safe to expose; access controlled by Firebase Auth) and
  `PUBLIC_API_URL`. Values are taken from the `academic-tok` web config in
  `./firebase_config` (apiKey, authDomain `academic-tok.firebaseapp.com`,
  projectId `academic-tok`, etc.).
- Add `.gitignore` for `.env`, `frontend/.env`, `authkey.json`, `secrets/`,
  `*.db`, `__pycache__/`, `frontend/node_modules/`, `frontend/dist/`.
- **Firebase project setup** — project `academic-tok` already exists and the web
  client config is provided. Two remaining one-time steps **by you**:
  (1) enable the **Google** sign-in provider in the Firebase console if not
  already on; (2) download the Admin SDK **service-account key** to `authkey.json`
  (the web config in `./firebase_config` is the client config, not the Admin key
  the backend needs for `verify_id_token`).

---

## 8. Build order (proposed milestones)

1. **Scaffolding** — repo layout, `requirements.txt`, `config.py`, `.env.example`,
   `.gitignore`, `db.py` + schema (incl. `users`/`subscriptions`), `seeds.py`.
2. **Semantic Scholar client** with rate limiting + a smoke-test script.
3. **Ollama summarizer + classifier** with the structured prompts.
4. **Ingestion pipeline** end-to-end; seed the DB with a few fields' papers.
5. **Flask API (public)** — fields + single-field + paper detail + mixed feed
   reading from SQLite (no auth required yet).
6. **Auth layer** — port `auth.py` (firebase_admin init + `_verify_token`),
   `/api/verifyUserId`, subscriptions endpoints, and the auth-gated subscribed feed.
7. **Astro frontend** — port `firebase.ts`/`auth.ts`/`api.ts`, login page, feed
   scroll, subscribed/mixed toggle backed by the subscriptions API.
8. **Polish** — pagination/keyset, mixed-feed sampling, signed-out states, README
   with run instructions (incl. Firebase setup steps).

Ingestion (1–4) and the public API (5) are testable from the CLI/curl before any
auth or frontend work. Auth (6) can be smoke-tested with a real token before the
UI exists.

---

## 9. Dependencies

**Python** (`requirements.txt`): `flask`, `flask-cors`, `firebase-admin`,
`requests`, `python-dotenv`. (Stdlib `sqlite3` for the DB.) Optional: `pytest`
for tests.

**Frontend:** Astro + npm; `firebase` (client SDK) + `nanostores` for the auth
store; optionally Preact for the feed island.

---

## 10. Resolved decisions & remaining assumptions

**Resolved (per your comments):**

- **Ollama model** — `qwen3.5:9b` (set as the `OLLAMA_MODEL` default).
- **Firebase project** — new project `academic-tok`; web client config lives in
  `./firebase_config` and feeds the frontend `PUBLIC_FIREBASE_*` env.
- **Sign-in providers** — **Google only** for now.

**Remaining assumptions (no objection raised — proceeding as written):**

- **"Importance" heuristic** — v1 favors citation count + recency from the
  search results; can refine later (e.g. Semantic Scholar relevance ordering).
- **Ingestion trigger** — v1 is manual CLI. Background/scheduled refresh is a
  later add-on.
- **Field-sprawl control** — relying on prompt guardrails + slug de-duplication
  in v1; a merge/cleanup pass can come later if fields proliferate.

**One thing still needed from you before auth works end-to-end:** the Firebase
Admin **service-account key** saved as `authkey.json` (the backend needs it for
`verify_id_token`; the web config in `./firebase_config` is not sufficient).
```
