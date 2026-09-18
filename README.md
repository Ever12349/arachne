# arachne

Synchronous **URL → structured JSON** extract service for AI agents.

P3 fetches HTML with `httpx` (optional headless Playwright) and extracts `title` / `main_text` / metadata / links. Optional JSON site profiles in `ARACHNE_PROFILES_DIR` override CSS title/main/meta. POST `/extract` may inject caller `cookies` / allowlisted `headers`, reference a Fernet-encrypted `session_id`, set `ua_strategy`, set `render`, or force `site_profile`. In-process QPS, concurrency, and a 60s TTL cache are on. Default install has **no** browsers, queue, Redis, or database.

API contract version **0.4.0**.

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
uvicorn app.main:app
```

## Docker

Default image stays **browser-free**:

```bash
docker compose up --build -d
```

Playwright image (Chromium + system deps):

```bash
docker build -f Dockerfile.playwright -t arachne:playwright .
docker run --rm -p 8000:8000 \
  -e ARACHNE_SESSION_KEY \
  -v "$(pwd)/data/sessions:/app/data/sessions" \
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

Health check:

```bash
curl http://127.0.0.1:8000/health
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
  -v "$(pwd)/data/profiles:/app/data/profiles" \
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

Global extract QPS is a fixed 1-second window of 5 (override with `ARACHNE_QPS`). Cache misses (including Playwright) are also limited by `ARACHNE_MAX_CONCURRENCY` (default 10). `/health` and `/stats` are exempt.

## Stats

```bash
curl http://127.0.0.1:8000/stats
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
| `challenge_detected` | 403 |
| `unsupported_content`, `extract_empty`, `too_large` | 422 |
| `rate_limited` | 429 |
| `timeout` | 504 |
| `fetch_failed`, `unauthorized_upstream`, `render_failed` | 502 |
| `render_unavailable` | 501 |
| `internal` | 500 |

Upstream 401/403 become `unauthorized_upstream` unless the body looks like a challenge (`challenge_detected`). Other `status >= 400` become `fetch_failed`. Both include `detail.status_code`. Only `http`/`https` URLs are accepted; private, loopback, link-local, and unspecified addresses are rejected as `bad_url`. Cookie / Authorization / session plaintext is never written to logs.

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

`tests/test_profiles.py` also covers host match (including `www.`), lexicographic host conflicts, `strict` → `extract_empty`, `profile_fallback`, and cache keys that include `profile_id@version`.

Live integration (fetches `https://example.com`):

```bash
ARACHNE_INTEGRATION=1 pytest -m integration
```

## Health

```bash
curl http://127.0.0.1:8000/health
```

Design notes (SSRF, pipeline, roadmap): [docs/DESIGN.md](docs/DESIGN.md).
