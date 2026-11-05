"""Swagger UI for testing the API by hand, off on a server by default (WHO-58)."""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.main import docs_routes
from app.core.config import Settings

REQUIRED = {
    "database_url": "postgresql+psycopg://test:test@localhost:5432/test",
    "token_encryption_key": "unused",
    "session_token_secret": "unused",
    "app_callback_url": "new2u://signed-in",
}


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **{**REQUIRED, **values})  # type: ignore[call-arg]


def test_swagger_ui_is_served_locally(client: TestClient) -> None:
    response = client.get("/docs")

    assert response.status_code == 200
    assert "swagger-ui" in response.text
    assert client.get("/redoc").status_code == 404


def test_the_session_token_is_a_bearer_scheme_on_every_authenticated_route(
    client: TestClient,
) -> None:
    schema = client.get("/openapi.json").json()

    assert schema["components"]["securitySchemes"]["HTTPBearer"] == {
        "type": "http",
        "scheme": "bearer",
        "description": "The session token from POST /auth/spotify/login/{login_id}/claim.",
    }
    secured = {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        if operation.get("security") == [{"HTTPBearer": []}]
    }
    assert secured == {
        ("POST", "/auth/spotify/login"),  # optional: present means a renewal
        ("POST", "/auth/logout"),
        ("GET", "/v1/me/connection"),
        ("GET", "/v1/timeline"),
        ("GET", "/v1/summary"),
        ("GET", "/v1/new-to-you"),
        ("GET", "/v1/new-to-you/albums/{album_id}/tracks"),
        ("GET", "/v1/albums"),
        ("GET", "/v1/singles"),
        ("GET", "/v1/albums/{album_id}/tracks"),
        ("PUT", "/v1/new-to-you/not-new/{kind}/{item_id}"),
        ("DELETE", "/v1/new-to-you/not-new/{kind}/{item_id}"),
        ("POST", "/v1/new-to-you/not-new"),
        ("GET", "/v1/me/review"),
        ("POST", "/v1/me/review/done"),
        ("PUT", "/v1/albums/{album_id}/rating"),
        ("DELETE", "/v1/albums/{album_id}/rating"),
        ("PUT", "/v1/albums/{album_id}/comment"),
        ("DELETE", "/v1/albums/{album_id}/comment"),
        ("PUT", "/v1/tracks/{item_key}/comment"),
        ("DELETE", "/v1/tracks/{item_key}/comment"),
        ("GET", "/v1/ratings/albums"),
        ("GET", "/v1/search"),
        ("GET", "/v1/export"),
        ("DELETE", "/v1/me"),
        ("GET", "/v1/config"),
        ("POST", "/v1/me/spotify/poll"),
        ("POST", "/v1/spotify/history-import"),
        ("GET", "/v1/spotify/history-import"),
    }


def test_no_docs_and_no_schema_when_disabled() -> None:
    app = FastAPI(**docs_routes(False))
    client = TestClient(app)

    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(path).status_code == 404, path


@pytest.mark.parametrize(
    ("env", "override", "enabled"),
    [
        ("local", None, True),
        ("staging", None, False),
        ("production", None, False),
        ("production", True, True),
        ("local", False, False),
    ],
)
def test_docs_are_on_locally_and_off_on_a_server_unless_set(
    env: str, override: bool | None, enabled: bool
) -> None:
    assert settings(app_env=env, api_docs_enabled=override).docs_enabled is enabled
