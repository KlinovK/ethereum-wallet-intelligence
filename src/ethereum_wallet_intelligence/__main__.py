"""Executable application entry point."""

import uvicorn

from ethereum_wallet_intelligence.config import Settings
from ethereum_wallet_intelligence.main import create_app


def main() -> None:
    """Start the API using validated environment settings."""
    settings = Settings.from_environment()
    uvicorn.run(
        create_app(settings),
        host=settings.app_host,
        port=settings.app_port,
    )


if __name__ == "__main__":
    main()
