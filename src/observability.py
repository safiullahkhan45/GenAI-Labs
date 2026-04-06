"""Structured logging and observability for the analytics pipeline."""

from __future__ import annotations

import logging
import sys
import time
from contextlib import contextmanager
from typing import Any, Generator

_LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

_configured = False


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logger once; subsequent calls are no-ops."""
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(handler)
    root.setLevel(level)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a named logger, ensuring logging is configured."""
    setup_logging()
    return logging.getLogger(name)


@contextmanager
def timed_stage(logger: logging.Logger, stage: str) -> Generator[None, None, None]:
    """Log stage entry/exit with elapsed time. Re-raises exceptions."""
    logger.debug("stage_start stage=%s", stage)
    start = time.perf_counter()
    try:
        yield
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000
        logger.error("stage_error stage=%s elapsed_ms=%.1f error=%r", stage, elapsed, str(exc))
        raise
    else:
        elapsed = (time.perf_counter() - start) * 1000
        logger.debug("stage_end stage=%s elapsed_ms=%.1f", stage, elapsed)


def log_pipeline_result(logger: logging.Logger, result: Any) -> None:
    """Emit one structured INFO line summarising a completed pipeline run."""
    timings = getattr(result, "timings", {})
    stats = getattr(result, "total_llm_stats", {})
    sql_preview = ((getattr(result, "sql", None) or "").replace("\n", " "))[:80]
    logger.info(
        "pipeline_complete request_id=%s status=%s total_ms=%.1f "
        "llm_calls=%s total_tokens=%s sql=%r",
        getattr(result, "request_id", None),
        getattr(result, "status", "unknown"),
        timings.get("total_ms", 0.0),
        stats.get("llm_calls", "?"),
        stats.get("total_tokens", "?"),
        sql_preview,
    )