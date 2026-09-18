# arachne

Synchronous **URL → structured JSON** extract service for AI agents.

P1 fetches HTML with `httpx` and extracts `title` / `main_text` / metadata / links. POST `/extract` may inject caller `cookies` / allowlisted `headers`. In-process QPS, concurrency, and a 60s TTL cache are on. No Playwright, queue, Redis, or database.

API contract version **0.2.0**.

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
uvicorn app.main:app
```

## Docker

One-click (build and run in the background):

```bash
docker compose up --build -d
```

If Docker Hub is blocked (common in mainland China), overlay the mirror Compose file so the Python base image is pulled from a China-accessible mirror:

```bash
docker compose -f docker-compose.yml -f docker-compose.mirror.yml up --build -d
```

The overlay defaults to `docker.m.daocloud.io/library/python:3.12-slim`. Public mirrors change; if that host is down, pick another `library/python:3.12-slim` mirror (for example Aliyun) and override `PYTHON_IMAGE`.

Override the base image without editing files:

```bash
docker compose build --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
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

Same `PYTHON_IMAGE` override for a plain `docker build`:

```bash
docker build --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim -t arachne .
```

Pass `ARACHNE_USER_AGENT` and the other `ARACHNE_*` settings the same way as a local run (`-e` on `docker run`, or `environment:` in Compose). Do not bake credentials into the image.

## Extract

`GET` or `POST /extract`. Prefer `POST` when sending cookies or headers.

GET query: `url` (required), `max_chars` (optional). No cookies/headers.

POST JSON: `url`, optional `headers` and `cookies` (`dict[str, str]` only), optional `max_chars`.

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
  "truncated": false
}
```

`url` is the final URL after redirects. `requested_url` is what the caller sent. Metadata string fields default to `""`. `links` are absolute `http`/`https` anchors, same-host first (leading `www.` ignored), capped at 50. Bare `#` fragments, `tel:`, `javascript:`, `mailto:`, and non-http(s) hrefs are skipped. Link text is capped at 200 characters.

`main_text` is hard-capped at 100_000 characters. If you pass `max_chars`, it is clamped to `[1, 100000]` and `main_text` is cut to that length. `truncated` is `true` only when text was cut. There is no `cached` field on the success body; cache hits are counted in logs and `/stats` only.

POST headers are allowlisted: `Authorization`, `Accept`, `Accept-Language`, `User-Agent`, `Referer`, `Cache-Control`. `Host`, `Content-Length`, `Transfer-Encoding`, `Connection`, and `Cookie` are stripped (`Cookie` only via the `cookies` field). A caller `User-Agent` overrides `ARACHNE_USER_AGENT`.

Agent client timeout should be slightly above the server read timeout (15s); **≥ 20s** is a reasonable default.

Global extract QPS is a fixed 1-second window of 5 (override with `ARACHNE_QPS`). Cache misses are also limited by `ARACHNE_MAX_CONCURRENCY` (default 10). `/health` and `/stats` are exempt.

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
| `unsupported_content`, `extract_empty`, `too_large` | 422 |
| `rate_limited` | 429 |
| `timeout` | 504 |
| `fetch_failed`, `unauthorized_upstream` | 502 |
| `internal` | 500 |

Upstream 401/403 become `unauthorized_upstream`; other `status >= 400` become `fetch_failed`. Both include `detail.status_code`. Only `http`/`https` URLs are accepted; private, loopback, link-local, and unspecified addresses are rejected as `bad_url`. Cookie / Authorization values are never written to logs.

## Tests

Unit tests mock DNS and HTTP (no live network):

```bash
pytest -m "not integration"
```

Live integration (fetches `https://example.com`):

```bash
ARACHNE_INTEGRATION=1 pytest -m integration
```

## Health

```bash
curl http://127.0.0.1:8000/health
```

Design notes (SSRF, pipeline, roadmap): [docs/DESIGN.md](docs/DESIGN.md).
