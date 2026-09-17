# arachne

Python HTTP crawler service (FastAPI stub).

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
uvicorn app.main:app --reload
```

## Example

```bash
curl 'http://127.0.0.1:8000/extract?url=https://example.com'
```

Placeholder JSON contract:

```json
{
  "url": "https://example.com",
  "title": "",
  "main_text": "",
  "metadata": {},
  "links": []
}
```

Crawl/extract logic is not implemented yet.
