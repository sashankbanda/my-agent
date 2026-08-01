"""Process entrypoint: ``python -m myagent`` (or the ``myagent`` script).

Wires the boot sequence in its one canonical order:
settings -> logging -> ASGI app (whose lifespan migrates the DB) -> uvicorn.
"""

from __future__ import annotations

import uvicorn

from myagent.config import load_settings
from myagent.logging import configure_logging, get_logger
from myagent.server.app import create_app


def main() -> None:
    """Boot the kernel and serve until interrupted."""
    settings = load_settings()
    configure_logging(settings.logging)
    log = get_logger(__name__)
    log.info(
        "booting",
        host=settings.server.host,
        port=settings.server.port,
        data_dir=str(settings.app.resolved_data_dir()),
    )
    app = create_app(settings)
    uvicorn.run(app, host=settings.server.host, port=settings.server.port, log_config=None)


if __name__ == "__main__":
    main()
