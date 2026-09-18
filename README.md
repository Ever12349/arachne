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
