"""Phase 0 acceptance: the service comes up and answers."""

from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]


def test_openapi_is_generated(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200
