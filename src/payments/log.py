"""structlog setup. Stdlib loggers (werkzeug, gunicorn, google libs) share the same renderer."""

import logging
import sys
from typing import Literal

import structlog


def configure_logging(level: str = "INFO", fmt: Literal["json", "console"] = "json") -> None:
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    if fmt == "console":
        final: list[structlog.types.Processor] = [structlog.dev.ConsoleRenderer()]
    else:
        final = [structlog.processors.format_exc_info, structlog.processors.JSONRenderer()]

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *final],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())

    # The MarketPay client logs its own calls (with timing); httpx's INFO lines are duplicates.
    logging.getLogger("httpx").setLevel(logging.WARNING)
