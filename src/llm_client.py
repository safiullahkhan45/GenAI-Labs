from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from src.observability import get_logger
from src.types import SQLGenerationOutput, AnswerGenerationOutput

DEFAULT_MODEL = "openai/gpt-4o-mini"

logger = get_logger("llm_client")


class OpenRouterLLMClient:
    """LLM client using the OpenRouter SDK for chat completions."""

    provider_name = "openrouter"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        try:
            from openrouter import OpenRouter
        except ModuleNotFoundError as exc:
            raise RuntimeError("Missing dependency: install 'openrouter'.") from exc
        self.model = model or os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
        self._client = OpenRouter(api_key=api_key)
        self._stats: dict[str, Any] = {
            "llm_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    # ------------------------------------------------------------------
    # Core HTTP call
    # ------------------------------------------------------------------

    def _chat(self, messages: list[dict[str, str]], temperature: float, max_tokens: int) -> str:
        res = self._client.chat.send(
            messages=messages,
            model=self.model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
        )

        # Token counting — res.usage is ChatUsage with prompt_tokens,
        # completion_tokens, total_tokens (floats). Cast to int for contract.
        usage = getattr(res, "usage", None)
        if usage is not None:
            self._stats["prompt_tokens"]     += int(getattr(usage, "prompt_tokens",     0) or 0)
            self._stats["completion_tokens"] += int(getattr(usage, "completion_tokens", 0) or 0)
            self._stats["total_tokens"]      += int(getattr(usage, "total_tokens",      0) or 0)
        self._stats["llm_calls"] += 1

        choices = getattr(res, "choices", None) or []
        if not choices:
            raise RuntimeError("OpenRouter response contained no choices.")
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if not isinstance(content, str):
            raise RuntimeError("OpenRouter response content is not text.")
        return content.strip()

    # ------------------------------------------------------------------
    # SQL extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_sql(text: str) -> str | None:
        """
        Extract SQL from LLM output. Tries in order:
          1. Markdown code fence  ```sql\\nSELECT...\\n```
          2. JSON object          {"sql": "SELECT..."} or {"sql": null}
          3. Raw SELECT scan      first occurrence of SELECT keyword
        """
        stripped = text.strip()

        # 1. Markdown code fence
        fence = re.search(r"```(?:sql)?\s*\n(.*?)\n\s*```", stripped, re.DOTALL | re.IGNORECASE)
        if fence:
            candidate = fence.group(1).strip()
            if candidate.upper().startswith("SELECT"):
                return candidate

        # 2. JSON object
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                sql = parsed.get("sql")
                if sql is None:
                    return None  # LLM explicitly said it cannot answer
                if isinstance(sql, str) and sql.strip():
                    return sql.strip()
            except json.JSONDecodeError:
                pass

        # 3. Raw SELECT scan
        lower = stripped.lower()
        idx = lower.find("select ")
        if idx >= 0:
            raw = stripped[idx:]
            # Strip trailing prose / markdown that follows the SQL
            raw = re.split(r"\n```|\n\n[A-Z][a-z]", raw)[0].strip()
            return raw

        return None

    # ------------------------------------------------------------------
    # Schema formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _format_schema(context: dict) -> str:
        table = context.get("table", "gaming_mental_health")
        columns: list[tuple[str, str]] = context.get("columns", [])
        if not columns:
            return f"Table: {table}\n(schema unavailable)"
        col_lines = "\n".join(f"  - {name} ({dtype})" for name, dtype in columns)
        return f"Table: {table}\nColumns:\n{col_lines}"

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def generate_sql(self, question: str, context: dict) -> SQLGenerationOutput:
        schema_str = self._format_schema(context)

        system_prompt = (
            "You are a SQL assistant for a SQLite database.\n\n"
            f"{schema_str}\n\n"
            "Rules:\n"
            "  1. Only generate SELECT queries. Never write DELETE, DROP, INSERT, UPDATE, or ALTER.\n"
            "  2. Use exact column names listed above — do not invent columns.\n"
            "  3. Questions may use informal language like 'share' (meaning proportion/percentage),\n"
            "     'bucket' (meaning group/range), 'roughly' (meaning approximate), etc.\n"
            "     Interpret these as analytics questions and generate the appropriate SQL.\n"
            "  4. Only return {\"sql\": null} if the question genuinely cannot be answered\n"
            "     with ANY of the available columns — not just because the wording is informal.\n"
            "  5. If asked to modify or delete data, "
            'return exactly: {"sql": null}\n'
            "  6. Return only the SQL query with no explanation."
        )
        user_prompt = f"Question: {question}"

        start = time.perf_counter()
        error = None
        sql = None

        try:
            text = self._chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                temperature=0.0,
                max_tokens=300,
            )
            logger.debug("sql_raw_response preview=%r", text[:200])
            sql = self._extract_sql(text)
        except Exception as exc:
            error = str(exc)
            logger.error("sql_generation_failed error=%r", error)

        timing_ms = (time.perf_counter() - start) * 1000
        llm_stats = self.pop_stats()
        llm_stats["model"] = self.model

        return SQLGenerationOutput(
            sql=sql,
            timing_ms=timing_ms,
            llm_stats=llm_stats,
            error=error,
        )

    def generate_answer(
        self,
        question: str,
        sql: str | None,
        rows: list[dict[str, Any]],
    ) -> AnswerGenerationOutput:
        _zero_stats: dict[str, Any] = {
            "llm_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "model": self.model,
        }

        if not sql:
            return AnswerGenerationOutput(
                answer=(
                    "I cannot answer this with the available table and schema. "
                    "Please rephrase using known survey fields."
                ),
                timing_ms=0.0,
                llm_stats=_zero_stats,
            )

        if not rows:
            return AnswerGenerationOutput(
                answer="The query executed successfully but returned no results.",
                timing_ms=0.0,
                llm_stats=_zero_stats,
            )

        system_prompt = (
            "You are a concise analytics assistant. "
            "Use only the provided SQL results. Do not invent data."
        )
        user_prompt = (
            f"Question:\n{question}\n\n"
            f"SQL:\n{sql}\n\n"
            f"Results (JSON):\n{json.dumps(rows[:30], ensure_ascii=True)}\n\n"
            "Write a concise answer in plain English."
        )

        start = time.perf_counter()
        error = None
        answer = ""

        try:
            answer = self._chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                temperature=0.2,
                max_tokens=220,
            )
        except Exception as exc:
            error = str(exc)
            answer = f"Error generating answer: {error}"
            logger.error("answer_generation_failed error=%r", error)

        timing_ms = (time.perf_counter() - start) * 1000
        llm_stats = self.pop_stats()
        llm_stats["model"] = self.model

        return AnswerGenerationOutput(
            answer=answer,
            timing_ms=timing_ms,
            llm_stats=llm_stats,
            error=error,
        )

    def pop_stats(self) -> dict[str, Any]:
        out = dict(self._stats)
        self._stats = {
            "llm_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        return out


def build_default_llm_client() -> OpenRouterLLMClient:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required.")
    return OpenRouterLLMClient(api_key=api_key)
