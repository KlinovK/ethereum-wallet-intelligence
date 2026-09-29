"""FastAPI application bootstrap."""

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from ethereum_wallet_intelligence.config import Settings


class HealthResponse(BaseModel):
    """Health endpoint response contract."""

    status: Literal["ok"]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the ASGI application."""
    resolved_settings = settings if settings is not None else Settings()
    app = FastAPI(title=resolved_settings.app_name)

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    return app
