"""uvicorn entrypoint.

    python -m localoctop.main

All configuration comes from environment variables — see config.py and the
README for the full list.
"""

from __future__ import annotations

import uvicorn

from .app import create_app
from .config import load_settings


def main() -> None:
    settings = load_settings()
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        # WebSocket ping/pong keepalive at the uvicorn layer complements the
        # client's own pings and the idle-timeout sweep.
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )


if __name__ == "__main__":
    main()
