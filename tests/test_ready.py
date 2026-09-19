"""GET /ready: DB SELECT 1 + job worker task still alive."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_ready_ok(api_client: TestClient):
    response = api_client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_worker_dead_is_503(api_client: TestClient):
    api_client.app.state.job_worker._task = None
    response = api_client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "not_ready"
    assert "message" in body["error"]
    assert isinstance(body["error"]["detail"], dict)


def test_ready_db_failure_is_503(api_client: TestClient):
    class _FailingConnect:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *_args):
            return False

    api_client.app.state.db_engine.connect = lambda: _FailingConnect()
    response = api_client.get("/ready")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "not_ready"
