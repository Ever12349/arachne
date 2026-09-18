"""Optional live test against a public HTML page.

Run with:

    ARACHNE_INTEGRATION=1 pytest -m integration
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.integration

skip_live = pytest.mark.skipif(
    os.environ.get("ARACHNE_INTEGRATION") != "1",
    reason="Set ARACHNE_INTEGRATION=1 to run live tests",
)


@skip_live
def test_extract_example_com():
    with TestClient(app) as client:
        response = client.get("/extract", params={"url": "https://example.com"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["requested_url"] == "https://example.com"
    assert data["url"].startswith("http")
    assert data["status_code"] == 200
    assert data["title"]
    assert data["main_text"]
    assert data["metadata"]["content_type"]
    assert isinstance(data["links"], list)
