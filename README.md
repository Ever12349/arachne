# arachne

**URL → structured JSON** extract service for AI agents, with optional SQLite-backed batch jobs.

The service fetches HTML with `httpx` (optional headless Playwright) and extracts `title` / `main_text` / metadata / links. Optional JSON site profiles in `ARACHNE_PROFILES_DIR` override CSS title/main/meta. `POST /profiles/suggest` proposes a profile from a live page (no disk write); `POST /profiles` saves one (requires `ARACHNE_PROFILES_WRITE=1`). POST `/extract` may inject caller `cookies` / allowlisted `headers`, reference a Fernet-encrypted `session_id`, set `ua_strategy`, set `render`, or force `site_profile`. `POST /jobs` queues many URLs; poll `GET /jobs/{id}` (reads SQLite). Default install has **no** browsers, Redis, PostgreSQL, or webhooks. Job rows **survive process restart**; sessions stay Fernet files.

This is a **trusted single-instance / intranet sidecar**. Do not run multiple replicas (SQLite + in-process worker). Terminate TLS at a reverse proxy; the app speaks HTTP. Optional shared API keys are not OAuth and are not bound to a client.

API contract version **0.8.0**.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

For tests:

```bash
pip install -r requirements-dev.txt
```

Optional Playwright (only if you need `render: true`):

```bash
pip install -r requirements-playwright.txt
playwright install chromium
```

The default process still starts if Playwright is missing; `render: true` then returns `render_unavailable`.

## Run

```bash
uvicorn app.main:app --reload
```

Optional settings (defaults shown):

```bash
export ARACHNE_USER_AGENT="MyAgent/1.0"
export ARACHNE_MAX_CONCURRENCY=10
export ARACHNE_QPS=5
export ARACHNE_CACHE_TTL_SECONDS=60
export ARACHNE_CACHE_MAXSIZE=256
export ARACHNE_SESSIONS_DIR=./data/sessions
export ARACHNE_SESSION_KEY=""          # Fernet key; required to load session_id
export ARACHNE_MAX_RETRIES=2
export ARACHNE_RETRY_BACKOFF_SECONDS=0.5,1
export ARACHNE_RENDER_TIMEOUT=15
export ARACHNE_PROFILES_DIR=./data/profiles
export ARACHNE_LLM_BASE_URL=https://api.openai.com/v1
export ARACHNE_LLM_API_KEY=""        # required for strategy=llm; auto without key stays heuristic
export ARACHNE_LLM_MODEL=gpt-4o-mini
export ARACHNE_LLM_TIMEOUT=30
export ARACHNE_SUGGEST_MIN_TITLE_CHARS=2
export ARACHNE_SUGGEST_MIN_MAIN_CHARS=80
export ARACHNE_JOB_MAX_URLS=50
export ARACHNE_JOB_CONCURRENCY=3
export ARACHNE_DATABASE_URL=sqlite+aiosqlite:///./data/arachne.db
export ARACHNE_JOB_DB_TTL_SECONDS=604800  # 7 days; 0 = keep completed jobs forever
export ARACHNE_REQUIRE_AUTH=false
export ARACHNE_API_KEYS=""               # comma-separated; required if REQUIRE_AUTH=true
export ARACHNE_METRICS_PUBLIC=false      # /metrics anonymous only when REQUIRE_AUTH and this is true
export ARACHNE_STATS_PUBLIC=false
export ARACHNE_PROFILES_WRITE=false      # POST /profiles needs this even when auth is off
export ARACHNE_EGRESS_ALLOWLIST=""       # comma-separated domain suffixes; empty = off
export ARACHNE_LOG_JSON=false            # 1/true/yes → one JSON object per log line
uvicorn app.main:app
```

`ARACHNE_REQUIRE_AUTH=true` with an empty `ARACHNE_API_KEYS` **refuses to start**. Boolean env vars accept `1` / `true` / `yes` (case-insensitive).

### API keys

When `ARACHNE_REQUIRE_AUTH=true`, every route except `/health` and `/ready` needs a valid key. `/metrics` is anonymous only if `ARACHNE_METRICS_PUBLIC=1`; `/stats` only if `ARACHNE_STATS_PUBLIC=1`. Send either header:

```bash
export ARACHNE_REQUIRE_AUTH=true
export ARACHNE_API_KEYS='key-one,key-two'
curl -H 'Authorization: Bearer key-one' 'http://127.0.0.1:8000/extract?url=https://example.com'
curl -H 'X-Arachne-Key: key-two' 'http://127.0.0.1:8000/stats'
```

When auth is off, Authorization / `X-Arachne-Key` are ignored (wrong keys still work as anonymous).

### Ops warnings

- **Single instance only.** SQLite and the in-process job worker are not safe behind a replica count > 1.
- **TLS belongs on the reverse proxy.** This process stays HTTP.
- **Profile writes are gated.** `POST /profiles` needs `ARACHNE_PROFILES_WRITE=1` even with auth off.
- **Egress allowlist** (`ARACHNE_EGRESS_ALLOWLIST`) applies to crawl URLs only (extract / jobs / suggest / render), not `ARACHNE_LLM_BASE_URL`.

## Docker

Default image stays **browser-free**:

```bash
docker compose up --build -d
```

Compose mounts `./data:/app/data` so `arachne.db`, sessions, and profiles survive container recreation. The default `ARACHNE_DATABASE_URL` is `sqlite+aiosqlite:///./data/arachne.db`.

Playwright image (Chromium + system deps):

```bash
docker build -f Dockerfile.playwright -t arachne:playwright .
docker run --rm -p 8000:8000 \
  -e ARACHNE_SESSION_KEY \
  -v "$(pwd)/data:/app/data" \
  arachne:playwright
```

China mirror overlays work the same as the default image (`PYTHON_IMAGE`, `PIP_INDEX_URL`, `PIP_TRUSTED_HOST`). See below.

Docker Hub timeouts and PyPI timeouts are independent. A DaoCloud (or other) base-image pull can still fail later at `pip install` if `files.pythonhosted.org` is unreachable. Overlay the mirror Compose file to cover **both**: Python image from a Docker Hub mirror, packages from a PyPI mirror.

```bash
docker compose -f docker-compose.yml -f docker-compose.mirror.yml up --build -d
```

Defaults in the overlay: `PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim` and Aliyun pip (`https://mirrors.aliyun.com/pypi/simple`). Public mirrors change; swap hosts if one is down. Tsinghua (`https://pypi.tuna.tsinghua.edu.cn/simple`) may return HTTP 403 from some networks and Docker builds; it remains an alternate: `PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple` and `PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn`.

Override without editing files:

```bash
docker compose build \
  --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim \
  --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
  --build-arg PIP_TRUSTED_HOST=mirrors.aliyun.com
docker compose up -d
```

Health / ready:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
```

Extract example:

```bash
curl 'http://127.0.0.1:8000/extract?url=https://example.com'
```

Stop:

```bash
docker compose down
```

Without Compose:

```bash
docker build -t arachne .
docker run --rm -p 8000:8000 arachne
```

Same image and pip-index overrides for a plain `docker build`:

```bash
docker build \
  --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim \
  --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
  --build-arg PIP_TRUSTED_HOST=mirrors.aliyun.com \
  -t arachne .
```

Pass `ARACHNE_USER_AGENT` and the other `ARACHNE_*` settings the same way as a local run (`-e` on `docker run`, or `environment:` in Compose). Do not bake session keys or cookies into the image. Mount a profiles directory if you use site rules:

```bash
docker run --rm -p 8000:8000 \
  -e ARACHNE_PROFILES_DIR=/app/data/profiles \
  -v "$(pwd)/data:/app/data" \
  arachne
```

## Encrypted sessions

There is **no** session HTTP API. Write files with `scripts/write_session.py`.

Generate a Fernet key (url-safe base64) and keep it only in `ARACHNE_SESSION_KEY`:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
export ARACHNE_SESSION_KEY='…'
export ARACHNE_SESSIONS_DIR=./data/sessions
python scripts/write_session.py --id demo \
  --cookies '{"sid":"abc"}' \
  --headers '{"Authorization":"Bearer …"}'
```

That writes `./data/sessions/demo.bin`. POST `{"session_id":"demo", ...}` loads it. Session cookies/headers are the base; the JSON body overrides; then the header allowlist runs (`Cookie` still only via the cookies map).

`session_id` must match `^[A-Za-z0-9_-]{1,64}$`. Missing key, bad id, missing file, or decrypt failure all return `session_invalid` (400) with a generic message.

## Site profiles

Optional per-host CSS rules. Default dir is `./data/profiles` (empty is fine). **Do not** point `ARACHNE_PROFILES_DIR` at `profiles/examples/` — that tree is docs only. Copy a file from there into your profiles dir to enable it.

One JSON file per profile: `{profile_id}.json`. The id must match `^[A-Za-z0-9_-]{1,64}$` and the `id` field inside the file.

```json
{
  "id": "example-com",
  "version": "1",
  "hosts": ["example.com"],
  "title_selector": "h1.article-title",
  "main_selector": "article .content",
  "remove_selectors": [".ads", "nav"],
  "meta": { "description": "meta[name='description']" },
  "strict": false,
  "disable_links": false
}
```

Host matching lowercases and strips a leading `www.`. If two files claim the same host, the highest `profile_id` lexicographically wins (warning log). The directory is re-read when its mtime (or a `*.json` mtime) changes, with a 1s debounce. Broken JSON is skipped and those hosts use generic extract.

`POST /extract` may set `site_profile` to force an id. Missing or invalid explicit ids return `profile_invalid` (400). When omitted, the final URL host is auto-matched. GET cannot pass `site_profile` but still auto-matches.

Every success body includes `profile_id` and `profile_version` (`""` if none) and `profile_fallback` (`true` only when a profile was selected, selectors missed title and main, and trafilatura ran instead). `strict: true` turns that miss into `extract_empty` (422). `disable_links: true` sets `links` to `[]`. Selectors are CSS only; a bad selector fails that field, not the request.

Cache keys include `profile_id@version` as well as URL, session fingerprint, `render`, and `ua_strategy`. Bump `version` when you change selectors.

### Suggest a profile (`POST /profiles/suggest`)

Fetches the page (same session / headers / cookies / `render` / SSRF / QPS / extract semaphore as `/extract`) and returns a P3 profile **without writing disk**. `strategy` is `heuristic` (default), `llm`, or `auto`.

Heuristic scores the DOM, builds CSS selectors, and self-tests them with lxml: title ≥ `ARACHNE_SUGGEST_MIN_TITLE_CHARS` (2), main ≥ `ARACHNE_SUGGEST_MIN_MAIN_CHARS` (80), plus overlap with page text. `id` is the normalized host with dots turned into dashes (`www.example.com` → `example-com`).

`llm` calls an OpenAI-compatible `POST {ARACHNE_LLM_BASE_URL}/chat/completions` with `ARACHNE_LLM_API_KEY` and `ARACHNE_LLM_MODEL`. The prompt includes a DOM skeleton (~200 nodes / ~30k chars) and the heuristic draft. Parsed JSON selectors **must** pass the same lxml self-test. No key → `llm_unavailable` (501). Call or verify failure → `llm_failed` (502). `auto` without a key stays heuristic and sets `evidence.llm_skipped=no_key`; with a key, LLM failure falls back to heuristic and sets `llm_skipped=llm_failed`.

One primary `profile` plus up to three `evidence.alternatives` (selector variants).

```bash
curl -X POST http://127.0.0.1:8000/profiles/suggest \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/article","strategy":"heuristic"}'
```

```json
{
  "profile": {
    "id": "example-com",
    "version": "1",
    "hosts": ["example.com"],
    "title_selector": "h1.headline",
    "main_selector": "div.entry-content",
    "remove_selectors": ["nav", ".ads"],
    "meta": {"description": "meta[name='description']"},
    "strict": false,
    "disable_links": false
  },
  "evidence": {
    "strategy_used": "heuristic",
    "title_preview": "Suggested Article Headline",
    "main_preview": "This is the unique main article body…"
  }
}
```

### Write a profile (`POST /profiles`)

Validates the full P3 schema and writes `ARACHNE_PROFILES_DIR/{id}.json`. Hand-authored profiles are fine (no prior suggest). Global directory only — no per-client subdirs. After a successful write the in-memory registry reloads immediately.

`POST /profiles` also requires `ARACHNE_PROFILES_WRITE=1` (default off), even when auth is disabled. Otherwise the response is `forbidden` (403).

If `{id}.json` already exists and `overwrite` is not `true`, the response is `profile_exists` (409). Pass `"overwrite": true` to replace.

```bash
curl -X POST http://127.0.0.1:8000/profiles \
  -H 'Content-Type: application/json' \
  -d '{"profile":{"id":"example-com","version":"1","hosts":["example.com"],"title_selector":"h1.article-title","main_selector":"article .content","remove_selectors":[".ads","nav"],"meta":{"description":"meta[name=\'description\']"},"strict":false,"disable_links":false},"overwrite":false}'
```

## Extract

`GET` or `POST /extract`. Prefer `POST` when sending cookies, headers, `session_id`, `ua_strategy`, `render`, or `site_profile`.

GET query: `url` (required), `max_chars` (optional). No cookies/headers/session/render/`site_profile`.

POST JSON: `url`, optional `headers` and `cookies` (`dict[str, str]` only), optional `max_chars`, optional `session_id`, optional `ua_strategy` (`default` | `rotate`), optional `render` (bool, default `false`), optional `site_profile` (profile id).

```bash
curl 'http://127.0.0.1:8000/extract?url=https://example.com'
```

```bash
curl 'http://127.0.0.1:8000/extract?url=https://example.com&max_chars=2000'
```

```bash
curl -X POST http://127.0.0.1:8000/extract \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com"}'
```

```bash
curl -X POST http://127.0.0.1:8000/extract \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","cookies":{"session":"…"},"headers":{"Authorization":"Bearer …","User-Agent":"MyAgent/1.0"},"max_chars":4000}'
```

```bash
curl -X POST http://127.0.0.1:8000/extract \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","session_id":"demo","ua_strategy":"rotate"}'
```

```bash
curl -X POST http://127.0.0.1:8000/extract \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","render":true}'
```

```bash
curl -X POST http://127.0.0.1:8000/extract \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","site_profile":"example-com"}'
```

Success (200):

```json
{
  "url": "https://example.com/",
  "requested_url": "https://example.com",
  "status_code": 200,
  "title": "Example Domain",
  "main_text": "…readable text…",
  "metadata": {
    "description": "",
    "language": "",
    "content_type": "text/html; charset=UTF-8",
    "og": {
      "title": "",
      "description": "",
      "image": ""
    }
  },
  "links": [
    {"href": "https://www.iana.org/domains/example", "text": "More information..."}
  ],
  "truncated": false,
  "profile_id": "",
  "profile_version": "",
  "profile_fallback": false
}
```

`url` is the final URL after redirects. `requested_url` is what the caller sent. Metadata string fields default to `""`. `links` are absolute `http`/`https` anchors, same-host first (leading `www.` ignored), capped at 50. Bare `#` fragments, `tel:`, `javascript:`, `mailto:`, and non-http(s) hrefs are skipped. Link text is capped at 200 characters.

`main_text` is hard-capped at 100_000 characters. If you pass `max_chars`, it is clamped to `[1, 100000]` and `main_text` is cut to that length. `truncated` is `true` only when text was cut. There is no `cached` field on the success body; cache hits are counted in logs and `/stats` only. Cache keys include the merged session fingerprint, `render`, `ua_strategy`, and `profile_id@version`.

POST headers are allowlisted: `Authorization`, `Accept`, `Accept-Language`, `User-Agent`, `Referer`, `Cache-Control`. `Host`, `Content-Length`, `Transfer-Encoding`, `Connection`, and `Cookie` are stripped (`Cookie` only via the `cookies` field or session `cookies`). A caller or session `User-Agent` overrides `ARACHNE_USER_AGENT` and `ua_strategy=rotate`.

Timeouts and connection errors are retried (`ARACHNE_MAX_RETRIES=2`, backoff 0.5s then 1s). 4xx and challenge pages are not retried. 2xx HTML or 401/403 bodies that match high-precision challenge markers (`cf-challenge`, `_cf_chl`, `just a moment`, `attention required`, …) return `challenge_detected` (403). Other 401/403 stay `unauthorized_upstream` (502).

Agent client timeout should be slightly above the server read timeout (15s); **≥ 20s** is a reasonable default. Leave more room when `render=true`.

Global extract QPS is a fixed 1-second window of 5 (override with `ARACHNE_QPS`). Cache misses (including Playwright) are also limited by `ARACHNE_MAX_CONCURRENCY` (default 10). `/health`, `/ready`, `/stats`, `/metrics`, **creating** a job (`POST /jobs`), and `POST /profiles` (write) are exempt. `POST /profiles/suggest` **does** consume QPS and the extract semaphore. Each job item still goes through `run_extract` and therefore the limiter.

## Batch jobs

Submit a list of URLs, then poll. There is **no** `GET /jobs` list and **no** webhooks. Jobs live in SQLite (`ARACHNE_DATABASE_URL`, default `sqlite+aiosqlite:///./data/arachne.db`). A process restart keeps job rows; `GET /jobs/{id}` reads the database. The in-process worker re-queues unfinished jobs on startup (interrupted `running` items go back to `pending`). Sessions are **not** stored in the DB.

Optional header `X-Arachne-Client` sets `client_id` (default `"default"`). `GET /jobs/{id}`, `DELETE /jobs/{id}`, `GET /jobs/search`, and cancel only see that client's jobs. A mismatch returns `job_not_found` (404) so another tenant cannot probe ids.

`ARACHNE_JOB_DB_TTL_SECONDS` defaults to `604800` (7 days). `0` keeps completed/cancelled jobs forever. A positive value deletes terminal jobs older than that many seconds.

`items` length is `1..ARACHNE_JOB_MAX_URLS` (default 50). Per-item fields inherit `defaults` when omitted. Each item calls the same `run_extract` path as `POST /extract` (QPS, cache, profiles, render). A job runs with `ARACHNE_JOB_CONCURRENCY` (default 3) item tasks at a time.

Create (HTTP 202):

```bash
curl -X POST http://127.0.0.1:8000/jobs \
  -H 'Content-Type: application/json' \
  -d '{"defaults":{"ua_strategy":"default","render":false,"site_profile":null},"items":[{"url":"https://example.com"},{"url":"https://example.com/other","render":true}]}'
```

```json
{"job_id":"3fa85f64-5717-4562-b3fc-2c963f66afa6","status":"queued","total":2}
```

Poll until `status` is `completed` or `cancelled`:

```bash
JOB_ID=3fa85f64-5717-4562-b3fc-2c963f66afa6
curl "http://127.0.0.1:8000/jobs/$JOB_ID"
curl -H 'X-Arachne-Client: my-agent' "http://127.0.0.1:8000/jobs/$JOB_ID"
```

Job statuses: `queued` | `running` | `completed` | `cancelled`. Item statuses: `pending` | `running` | `succeeded` | `failed` | `cancelled`. Counts: `total`, `succeeded_count`, `failed_count`, `cancelled_count`. A succeeded item inlines a full extract body as `result`. A failed item uses the same error envelope as the sync API:

```json
{
  "index": 1,
  "url": "http://127.0.0.1/",
  "status": "failed",
  "result": {
    "error": {
      "code": "bad_url",
      "message": "…",
      "detail": {}
    }
  }
}
```

One failed item does **not** fail the job: remaining items finish and the job is `completed`.

Cancel pending items (`running` items finish and record a result). Already-terminal jobs return 200 with the same body:

```bash
curl -X POST "http://127.0.0.1:8000/jobs/$JOB_ID/cancel"
```

Search by exact item `requested_url` or the extract result's final `url` (`limit` default 20, max 100):

```bash
curl 'http://127.0.0.1:8000/jobs/search?url=https://example.com&limit=20'
```

Delete (HTTP 204), same client only:

```bash
curl -X DELETE "http://127.0.0.1:8000/jobs/$JOB_ID"
```

Unknown ids or a different `X-Arachne-Client` return `job_not_found` (404).

## Stats and metrics

```bash
curl http://127.0.0.1:8000/stats
curl http://127.0.0.1:8000/metrics
```

```json
{
  "requests_total": 0,
  "errors_by_code": {},
  "cache_hits": 0,
  "cache_misses": 0,
  "in_flight": 0,
  "latency_ms_sum": 0.0,
  "latency_ms_count": 0
}
```

## Errors

Body is always:

```json
{
  "error": {
    "code": "bad_url",
    "message": "short explanation",
    "detail": {}
  }
}
```

Branch on `error.code`, not on `message`.

| code | HTTP |
|------|------|
| `bad_url` | 400 |
| `session_invalid` | 400 |
| `profile_invalid` | 400 |
| `profile_exists` | 409 |
| `unauthorized` | 401 |
| `forbidden` | 403 |
| `egress_blocked` | 403 |
| `challenge_detected` | 403 |
| `not_ready` | 503 |
| `unsupported_content`, `extract_empty`, `too_large` | 422 |
| `rate_limited` | 429 |
| `job_not_found` | 404 |
| `timeout` | 504 |
| `fetch_failed`, `unauthorized_upstream`, `render_failed`, `llm_failed` | 502 |
| `render_unavailable`, `llm_unavailable` | 501 |
| `internal` | 500 |

Upstream 401/403 become `unauthorized_upstream` unless the body looks like a challenge (`challenge_detected`). Other `status >= 400` become `fetch_failed`. Both include `detail.status_code`. Only `http`/`https` URLs are accepted; private, loopback, link-local, and unspecified addresses are rejected as `bad_url`. Optional `ARACHNE_EGRESS_ALLOWLIST` rejects other crawl hosts as `egress_blocked`. Cookie / Authorization / session plaintext is never written to logs. Set `X-Request-Id` to correlate a request; the value is echoed on the response.

## Tests

Unit tests mock DNS, HTTP, and Playwright (no live network, no browsers required):

```bash
pytest -m "not integration"
```

How the new P2/P3 codes are verified without a real site or browser:

| code | What the tests do |
|------|-------------------|
| `session_invalid` | POST `session_id` with a bad id, empty `ARACHNE_SESSION_KEY`, missing `{id}.bin`, or a file encrypted under another Fernet key (`tests/test_sessions.py`) |
| `challenge_detected` | Mock httpx to return `tests/fixtures/challenge_cf.html` / `challenge_attention.html` as 200 or 403 (`tests/test_antibot.py`) |
| `render_unavailable` | `monkeypatch` `app.render.playwright_available` to `False` and POST `"render": true` (`tests/test_render.py`) |
| `profile_invalid` | POST `site_profile` with a missing id or illegal id (`tests/test_profiles.py`) |
| `profile_exists` | POST `/profiles` twice without `overwrite` (`tests/test_profiles_write.py`) |
| `llm_unavailable` | POST `/profiles/suggest` with `"strategy":"llm"` and empty `ARACHNE_LLM_API_KEY` (`tests/test_profiles_suggest.py`) |
| `llm_failed` | Mock LLM selectors that fail lxml self-test with `"strategy":"llm"` (`tests/test_profiles_suggest.py`) |
| `job_not_found` | `GET` or cancel an unknown / expired job id (`tests/test_jobs.py`) |

`tests/test_jobs.py` covers create / poll / cancel / per-item errors / QPS. `tests/test_jobs_persistence.py` covers SQLite reopen, URL search, delete, `X-Arachne-Client` isolation, and TTL cleanup. `tests/test_profiles_suggest.py` covers heuristic suggest, `llm_unavailable`, mocked LLM verify/fail, auto fallback, and that suggest does not write disk. `tests/test_profiles_write.py` covers 409 / overwrite / hot-reload after write and the `ARACHNE_PROFILES_WRITE` gate. `tests/test_auth.py`, `tests/test_ready.py`, `tests/test_egress.py`, and `tests/test_transport.py` cover P7a auth, readiness, allowlist, and pin-IP.

`tests/test_jobs.py` covers create 202, per-item failure still `completed`, cancel, `job_not_found`, max URLs, and that creating a job does not burn QPS.

Live integration (fetches `https://example.com`):

```bash
ARACHNE_INTEGRATION=1 pytest -m integration
```

## Health

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
```

`/health` is liveness (`{status: ok}`). `/ready` checks SQLite (`SELECT 1`) and that the job worker task is still running; failure is `not_ready` (503).

Design notes (SSRF, pin-IP, pipeline, roadmap): [docs/DESIGN.md](docs/DESIGN.md).
