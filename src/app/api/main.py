"""FastAPI application.

Run with: uvicorn app.api.main:app
"""

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from app.api import auth, history_import, poll, privacy, read
from app.api.access_log import log_request
from app.api.no_store import no_store_on_private_routes
from app.auth.login import warn_if_no_account_may_sign_in
from app.core.config import get_settings
from app.core.log import configure_logging

# Without a root handler, app INFO lines (account_deleted among them) go nowhere.
configure_logging()
warn_if_no_account_may_sign_in(get_settings().spotify_account_allowlist)


def docs_routes(enabled: bool) -> dict[str, Any]:
    """Swagger UI at /docs and its schema at /openapi.json, or neither (WHO-58).

    ReDoc stays off: one docs page is enough for testing by hand.
    """
    if enabled:
        return {"docs_url": "/docs", "openapi_url": "/openapi.json", "redoc_url": None}
    return {"docs_url": None, "openapi_url": None, "redoc_url": None}


# FastAPI now ships OpenTelemetry hooks. Off: its spans and logs can carry
# request URLs and exception text (PRD P12), and nothing here exports them.
app = FastAPI(
    title="new2u",
    version="0.1.0",
    **docs_routes(get_settings().docs_enabled),
    telemetry={
        "tracing": False,
        "metrics": False,
        "logs": False,
        "operation_spans": False,
        "auto_configure": False,
    },
)
app.middleware("http")(log_request)
app.middleware("http")(no_store_on_private_routes)
app.include_router(auth.router)
app.include_router(privacy.router)
app.include_router(read.router)
app.include_router(poll.router)
app.include_router(history_import.router)


class HealthResponse(BaseModel):
    status: str


@app.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    """Liveness probe.

    Deliberately does not touch the database: this answers "is the API process
    up", which is what compose and a future restart policy need. A readiness
    check that verifies Postgres is a separate endpoint and a separate task.
    """
    return HealthResponse(status="ok")
