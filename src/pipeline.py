from __future__ import annotations

import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from src.llm_client import OpenRouterLLMClient, build_default_llm_client
from src.observability import get_logger, timed_stage, log_pipeline_result
from src.types import (
    SQLValidationOutput,
    SQLExecutionOutput,
    SQLGenerationOutput,
    AnswerGenerationOutput,
    PipelineOutput,
)

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = BASE_DIR / "data" / "gaming_mental_health.sqlite"
DEFAULT_TABLE_NAME = "gaming_mental_health"

logger = get_logger("pipeline")

# ---------------------------------------------------------------------------
# Destructive-intent pre-check
# ---------------------------------------------------------------------------

_DESTRUCTIVE_PATTERN = re.compile(
    r"\b(delete|drop|truncate|insert|update|alter|remove\s+all|wipe|erase)\b",
    re.IGNORECASE,
)


def _is_destructive(question: str) -> bool:
    """Return True if the question requests a data-modification operation."""
    return bool(_DESTRUCTIVE_PATTERN.search(question))


# ---------------------------------------------------------------------------
# SQL Validator
# ---------------------------------------------------------------------------

_DANGEROUS_STMT = re.compile(
    r"\b(DELETE|DROP|INSERT|UPDATE|TRUNCATE|ALTER|CREATE|REPLACE|ATTACH|DETACH|PRAGMA|VACUUM)\b",
    re.IGNORECASE,
)


class SQLValidationError(Exception):
    pass


class SQLValidator:
    """
    Validates that a SQL string is safe to execute.

    Checks (in order):
      1. Not None / not empty
      2. Starts with SELECT
      3. No dangerous DML/DDL keywords
      4. SQLite EXPLAIN — catches syntax errors and unknown columns/tables
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path

    def validate(self, sql: str | None) -> SQLValidationOutput:
        start = time.perf_counter()

        def _reject(reason: str) -> SQLValidationOutput:
            logger.debug("sql_rejected reason=%r sql=%r", reason, (sql or "")[:80])
            return SQLValidationOutput(
                is_valid=False,
                validated_sql=None,
                error=reason,
                timing_ms=(time.perf_counter() - start) * 1000,
            )

        # 1. None / empty
        if not sql or not sql.strip():
            return _reject("No SQL provided")

        stripped = sql.strip()

        # 2. Must start with SELECT
        if not stripped.upper().startswith("SELECT"):
            return _reject("Only SELECT queries are permitted")

        # 3. No dangerous DML/DDL keywords
        danger = _DANGEROUS_STMT.search(stripped)
        if danger:
            return _reject(f"Dangerous keyword not permitted: {danger.group().upper()}")

        # 4. EXPLAIN — validates full syntax, table names, and column names
        if self.db_path and Path(self.db_path).exists():
            try:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(f"EXPLAIN {stripped}")
            except sqlite3.Error as exc:
                return _reject(f"SQL syntax/schema error: {exc}")

        logger.debug("sql_valid sql=%r", stripped[:80])
        return SQLValidationOutput(
            is_valid=True,
            validated_sql=stripped,
            error=None,
            timing_ms=(time.perf_counter() - start) * 1000,
        )


# ---------------------------------------------------------------------------
# SQL Executor
# ---------------------------------------------------------------------------

class SQLiteExecutor:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def run(self, sql: str | None) -> SQLExecutionOutput:
        start = time.perf_counter()

        if sql is None:
            return SQLExecutionOutput(
                rows=[],
                row_count=0,
                timing_ms=(time.perf_counter() - start) * 1000,
                error=None,
            )

        error = None
        rows: list[dict[str, Any]] = []
        row_count = 0

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(sql)
                rows = [dict(r) for r in cur.fetchmany(100)]
                row_count = len(rows)
        except Exception as exc:
            error = str(exc)
            logger.warning("sql_execution_error error=%r sql=%r", error, sql[:80])
            rows = []
            row_count = 0

        return SQLExecutionOutput(
            rows=rows,
            row_count=row_count,
            timing_ms=(time.perf_counter() - start) * 1000,
            error=error,
        )


# ---------------------------------------------------------------------------
# Analytics Pipeline
# ---------------------------------------------------------------------------

class AnalyticsPipeline:
    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        llm_client: OpenRouterLLMClient | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.llm = llm_client or build_default_llm_client()
        self.executor = SQLiteExecutor(self.db_path)
        self.validator = SQLValidator(self.db_path)
        self._schema = self._load_schema()
        logger.info(
            "pipeline_init db=%s table=%s columns=%d",
            self.db_path.name,
            self._schema.get("table", "?"),
            len(self._schema.get("columns", [])),
        )

    def _load_schema(self) -> dict[str, Any]:
        """Load and cache table schema at startup — no repeated DB hits per request."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute(f"PRAGMA table_info({DEFAULT_TABLE_NAME})")
                rows = cur.fetchall()
                columns = [(r[1], r[2]) for r in rows]  # (name, type)
            return {"table": DEFAULT_TABLE_NAME, "columns": columns}
        except Exception as exc:
            logger.warning("schema_load_failed error=%r", str(exc))
            return {}

    def _zero_stats(self) -> dict[str, Any]:
        return {
            "llm_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "model": self.llm.model,
        }

    def _destructive_rejection(
        self,
        question: str,
        request_id: str,
        total_start: float,
    ) -> PipelineOutput:
        """Fully-formed PipelineOutput for a destructive request — zero LLM calls."""
        total_ms = (time.perf_counter() - total_start) * 1000
        timings = {
            "sql_generation_ms": 0.0,
            "sql_validation_ms": 0.0,
            "sql_execution_ms": 0.0,
            "answer_generation_ms": 0.0,
            "total_ms": total_ms,
        }
        validation = SQLValidationOutput(
            is_valid=False,
            validated_sql=None,
            error="Destructive operation not permitted: query modifies or deletes data",
            timing_ms=0.0,
        )
        answer_text = (
            "I cannot answer this with the available table and schema. "
            "Data modification requests are not permitted."
        )
        return PipelineOutput(
            status="invalid_sql",
            question=question,
            request_id=request_id,
            sql_generation=SQLGenerationOutput(
                sql=None,
                timing_ms=0.0,
                llm_stats=self._zero_stats(),
                error="Request blocked before SQL generation (destructive intent)",
            ),
            sql_validation=validation,
            sql_execution=SQLExecutionOutput(rows=[], row_count=0, timing_ms=0.0, error=None),
            answer_generation=AnswerGenerationOutput(
                answer=answer_text,
                timing_ms=0.0,
                llm_stats=self._zero_stats(),
            ),
            sql=None,
            rows=[],
            answer=answer_text,
            timings=timings,
            total_llm_stats=self._zero_stats(),
        )

    def run(self, question: str, request_id: str | None = None) -> PipelineOutput:
        total_start = time.perf_counter()
        rid = request_id or str(uuid.uuid4())[:8]

        logger.info("pipeline_start request_id=%s question=%r", rid, question[:120])

        # ------------------------------------------------------------------
        # Pre-check: reject destructive requests before touching the LLM
        # ------------------------------------------------------------------
        if _is_destructive(question):
            logger.warning("destructive_blocked request_id=%s question=%r", rid, question[:120])
            result = self._destructive_rejection(question, rid, total_start)
            log_pipeline_result(logger, result)
            return result

        # ------------------------------------------------------------------
        # Stage 1: SQL Generation
        # ------------------------------------------------------------------
        with timed_stage(logger, "sql_generation"):
            sql_gen_output = self.llm.generate_sql(question, self._schema)
        sql = sql_gen_output.sql

        # ------------------------------------------------------------------
        # Stage 2: SQL Validation
        # ------------------------------------------------------------------
        with timed_stage(logger, "sql_validation"):
            validation_output = self.validator.validate(sql)
        if not validation_output.is_valid:
            sql = None

        # ------------------------------------------------------------------
        # Stage 3: SQL Execution
        # ------------------------------------------------------------------
        with timed_stage(logger, "sql_execution"):
            execution_output = self.executor.run(sql)
        rows = execution_output.rows

        # ------------------------------------------------------------------
        # Stage 4: Answer Generation
        # ------------------------------------------------------------------
        with timed_stage(logger, "answer_generation"):
            answer_output = self.llm.generate_answer(question, sql, rows)

        # ------------------------------------------------------------------
        # Status determination
        # ------------------------------------------------------------------
        if sql_gen_output.sql is None and sql_gen_output.error:
            status = "unanswerable"
        elif not validation_output.is_valid:
            status = "invalid_sql"
        elif execution_output.error:
            status = "error"
        elif sql is None:
            status = "unanswerable"
        else:
            status = "success"

        # ------------------------------------------------------------------
        # Aggregates
        # ------------------------------------------------------------------
        total_ms = (time.perf_counter() - total_start) * 1000
        timings = {
            "sql_generation_ms":    sql_gen_output.timing_ms,
            "sql_validation_ms":    validation_output.timing_ms,
            "sql_execution_ms":     execution_output.timing_ms,
            "answer_generation_ms": answer_output.timing_ms,
            "total_ms":             total_ms,
        }

        def _sum(key: str) -> int:
            return (
                int(sql_gen_output.llm_stats.get(key, 0))
                + int(answer_output.llm_stats.get(key, 0))
            )

        total_llm_stats: dict[str, Any] = {
            "llm_calls":         _sum("llm_calls"),
            "prompt_tokens":     _sum("prompt_tokens"),
            "completion_tokens": _sum("completion_tokens"),
            "total_tokens":      _sum("total_tokens"),
            "model":             sql_gen_output.llm_stats.get("model", self.llm.model),
        }

        result = PipelineOutput(
            status=status,
            question=question,
            request_id=rid,
            sql_generation=sql_gen_output,
            sql_validation=validation_output,
            sql_execution=execution_output,
            answer_generation=answer_output,
            sql=sql,
            rows=rows,
            answer=answer_output.answer,
            timings=timings,
            total_llm_stats=total_llm_stats,
        )

        log_pipeline_result(logger, result)
        return result
