"""Structured logging setup (structlog).

One configuration call at boot; every module then uses ``get_logger(__name__)``.
JSON output for production, pretty console output for development - switched by
``Settings.logging.json``. Log records never contain secrets or raw prompts.
"""

from __future__ import annotations

import logging as stdlib_logging
import sys

import structlog

from myagent.config import LoggingSettings


def configure_logging(settings: LoggingSettings) -> None:
    """Configure stdlib + structlog once, at process start."""
    level = getattr(stdlib_logging, settings.level.upper(), stdlib_logging.INFO)
    stdlib_logging.basicConfig(level=level, stream=sys.stderr, format="%(message)s")

    renderer: structlog.types.Processor
    if settings.format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a named bound logger."""
    return structlog.get_logger(name)
