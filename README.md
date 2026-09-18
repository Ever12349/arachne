# arachne

Synchronous **URL → structured JSON** extract service for AI agents.

P0 fetches public HTML with `httpx` and extracts `title` / `main_text` / metadata / links. No cookies, Playwright, queue, or database.

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

Optional User-Agent override:

```bash
export ARACHNE_USER_AGENT="MyAgent/1.0"
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

Pass `ARACHNE_USER_AGENT` the same way as a local run (`-e` on `docker run`, or `environment:` in Compose). Do not bake credentials into the image.

## Extract

`GET` or `POST /extract`. Prefer `POST` so later optional fields do not hit URL-length limits.

```bash
curl 'http://127.0.0.1:8000/extract?url=https://example.com'
```

```bash
curl -X POST http://127.0.0.1:8000/extract \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com"}'
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
  ]
}
```

`url` is the final URL after redirects. `requested_url` is what the caller sent. Metadata string fields default to `""`. `links` are absolute `http`/`https` anchors, same-host first (leading `www.` ignored), capped at 50. `main_text` is capped at 100_000 characters.

Agent client timeout should be slightly above the server read timeout (15s); **≥ 20s** is a reasonable default.

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
| `timeout` | 504 |
| `fetch_failed`, `unauthorized_upstream` | 502 |
| `internal` | 500 |

`rate_limited` is not implemented in P0. Upstream 401/403 become `unauthorized_upstream`; other `status >= 400` become `fetch_failed`. Both include `detail.status_code`. Only `http`/`https` URLs are accepted; private, loopback, link-local, and unspecified addresses are rejected as `bad_url`.

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
