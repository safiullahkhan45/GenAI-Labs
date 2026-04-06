"""
Unit tests — no OPENROUTER_API_KEY required.

Tests cover:
  - SQL extraction from different LLM output formats
  - SQL validation logic
  - Destructive request pre-check
  - PipelineOutput contract shape (dataclass fields)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.llm_client import OpenRouterLLMClient
from src.pipeline import SQLValidator, _is_destructive
from src.types import PipelineOutput, SQLGenerationOutput, SQLValidationOutput, SQLExecutionOutput, AnswerGenerationOutput


# ---------------------------------------------------------------------------
# SQL Extraction Tests
# ---------------------------------------------------------------------------

class TestSQLExtraction(unittest.TestCase):

    def _extract(self, text: str):
        return OpenRouterLLMClient._extract_sql(text)

    def test_extracts_from_markdown_fence_with_lang(self):
        text = "Here is the query:\n```sql\nSELECT age FROM gaming_mental_health LIMIT 5\n```"
        result = self._extract(text)
        self.assertIsNotNone(result)
        self.assertTrue(result.upper().startswith("SELECT"))
        self.assertNotIn("```", result)

    def test_extracts_from_markdown_fence_no_lang(self):
        text = "```\nSELECT COUNT(*) FROM gaming_mental_health\n```"
        result = self._extract(text)
        self.assertIsNotNone(result)
        self.assertTrue(result.upper().startswith("SELECT"))

    def test_extracts_from_json_sql_key(self):
        text = '{"sql": "SELECT gender, AVG(addiction_level) FROM gaming_mental_health GROUP BY gender"}'
        result = self._extract(text)
        self.assertIsNotNone(result)
        self.assertTrue(result.upper().startswith("SELECT"))

    def test_returns_none_for_json_null(self):
        text = '{"sql": null}'
        result = self._extract(text)
        self.assertIsNone(result)

    def test_extracts_raw_select(self):
        text = "The answer to your question is: SELECT * FROM gaming_mental_health LIMIT 10"
        result = self._extract(text)
        self.assertIsNotNone(result)
        self.assertTrue(result.upper().startswith("SELECT"))

    def test_returns_none_for_garbage(self):
        text = "I don't know how to answer that question."
        result = self._extract(text)
        self.assertIsNone(result)

    def test_returns_none_for_empty_string(self):
        self.assertIsNone(self._extract(""))

    def test_does_not_extract_delete(self):
        text = "DELETE FROM gaming_mental_health WHERE id = 1"
        result = self._extract(text)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# SQL Validator Tests
# ---------------------------------------------------------------------------

class TestSQLValidator(unittest.TestCase):
    """Validation tests that don't require the DB (no EXPLAIN step)."""

    def setUp(self):
        # No db_path means EXPLAIN step is skipped — tests isolation logic only
        self.validator = SQLValidator(db_path=None)

    def test_rejects_none(self):
        out = self.validator.validate(None)
        self.assertFalse(out.is_valid)
        self.assertIsNotNone(out.error)

    def test_rejects_empty_string(self):
        out = self.validator.validate("")
        self.assertFalse(out.is_valid)
        self.assertIsNotNone(out.error)

    def test_rejects_delete_statement(self):
        out = self.validator.validate("DELETE FROM gaming_mental_health")
        self.assertFalse(out.is_valid)
        self.assertIsNotNone(out.error)

    def test_rejects_drop_statement(self):
        out = self.validator.validate("DROP TABLE gaming_mental_health")
        self.assertFalse(out.is_valid)
        self.assertIsNotNone(out.error)

    def test_rejects_insert_statement(self):
        out = self.validator.validate("INSERT INTO gaming_mental_health VALUES (1)")
        self.assertFalse(out.is_valid)
        self.assertIsNotNone(out.error)

    def test_rejects_update_statement(self):
        out = self.validator.validate("UPDATE gaming_mental_health SET age = 99")
        self.assertFalse(out.is_valid)
        self.assertIsNotNone(out.error)

    def test_rejects_non_select_pragma(self):
        out = self.validator.validate("PRAGMA table_info(gaming_mental_health)")
        self.assertFalse(out.is_valid)
        self.assertIsNotNone(out.error)

    def test_accepts_simple_select(self):
        out = self.validator.validate("SELECT * FROM gaming_mental_health LIMIT 1")
        self.assertTrue(out.is_valid)
        self.assertIsNone(out.error)
        self.assertIsNotNone(out.validated_sql)

    def test_accepts_aggregation_query(self):
        out = self.validator.validate(
            "SELECT gender, AVG(addiction_level) FROM gaming_mental_health GROUP BY gender"
        )
        self.assertTrue(out.is_valid)
        self.assertIsNone(out.error)

    def test_accepts_count_query(self):
        out = self.validator.validate(
            "SELECT COUNT(*) FROM gaming_mental_health WHERE addiction_level >= 5"
        )
        self.assertTrue(out.is_valid)

    def test_validated_sql_is_stripped(self):
        out = self.validator.validate("  SELECT 1  ")
        self.assertTrue(out.is_valid)
        self.assertEqual(out.validated_sql, "SELECT 1")

    def test_timing_ms_is_non_negative(self):
        out = self.validator.validate("SELECT 1")
        self.assertGreaterEqual(out.timing_ms, 0.0)


# ---------------------------------------------------------------------------
# Destructive Pre-Check Tests
# ---------------------------------------------------------------------------

class TestDestructivePreCheck(unittest.TestCase):

    def test_delete_question_is_flagged(self):
        self.assertTrue(_is_destructive("Please delete all rows from the table"))

    def test_drop_question_is_flagged(self):
        self.assertTrue(_is_destructive("Can you drop the gaming_mental_health table?"))

    def test_truncate_question_is_flagged(self):
        self.assertTrue(_is_destructive("Truncate the table"))

    def test_insert_question_is_flagged(self):
        self.assertTrue(_is_destructive("Insert a new row with age 25"))

    def test_update_question_is_flagged(self):
        self.assertTrue(_is_destructive("Update all records where addiction_level > 8"))

    def test_normal_analytics_question_not_flagged(self):
        self.assertFalse(_is_destructive("What is the average addiction level by gender?"))

    def test_top_n_question_not_flagged(self):
        self.assertFalse(_is_destructive("Show the top 5 age groups by anxiety score"))

    def test_count_question_not_flagged(self):
        self.assertFalse(_is_destructive("How many respondents have high addiction level?"))

    def test_distribution_question_not_flagged(self):
        self.assertFalse(_is_destructive("What is the addiction level distribution by gender?"))


# ---------------------------------------------------------------------------
# PipelineOutput Contract Tests (no network, no DB)
# ---------------------------------------------------------------------------

class TestPipelineOutputContract(unittest.TestCase):
    """Ensure PipelineOutput can be constructed and has all required fields."""

    def _make_output(self) -> PipelineOutput:
        stats = {"llm_calls": 1, "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "model": "test"}
        return PipelineOutput(
            status="success",
            question="test question",
            request_id="req-001",
            sql_generation=SQLGenerationOutput(sql="SELECT 1", timing_ms=100.0, llm_stats=stats),
            sql_validation=SQLValidationOutput(is_valid=True, validated_sql="SELECT 1", timing_ms=1.0),
            sql_execution=SQLExecutionOutput(rows=[{"val": 1}], row_count=1, timing_ms=5.0),
            answer_generation=AnswerGenerationOutput(answer="The answer is 1.", timing_ms=200.0, llm_stats=stats),
            sql="SELECT 1",
            rows=[{"val": 1}],
            answer="The answer is 1.",
            timings={
                "sql_generation_ms": 100.0,
                "sql_validation_ms": 1.0,
                "sql_execution_ms": 5.0,
                "answer_generation_ms": 200.0,
                "total_ms": 310.0,
            },
            total_llm_stats={"llm_calls": 2, "prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300, "model": "test"},
        )

    def test_status_is_valid_value(self):
        out = self._make_output()
        self.assertIn(out.status, {"success", "unanswerable", "invalid_sql", "error"})

    def test_timings_has_all_required_keys(self):
        out = self._make_output()
        for key in ("sql_generation_ms", "sql_validation_ms", "sql_execution_ms", "answer_generation_ms", "total_ms"):
            self.assertIn(key, out.timings)
            self.assertIsInstance(out.timings[key], (int, float))
            self.assertGreaterEqual(out.timings[key], 0.0)

    def test_total_llm_stats_has_all_required_keys(self):
        out = self._make_output()
        for key in ("llm_calls", "prompt_tokens", "completion_tokens", "total_tokens"):
            self.assertIn(key, out.total_llm_stats)
            self.assertIsInstance(out.total_llm_stats[key], int)
            self.assertGreaterEqual(out.total_llm_stats[key], 0)
        self.assertIn("model", out.total_llm_stats)
        self.assertIsInstance(out.total_llm_stats["model"], str)

    def test_stage_outputs_are_correct_types(self):
        out = self._make_output()
        self.assertIsInstance(out.sql_generation, SQLGenerationOutput)
        self.assertIsInstance(out.sql_validation, SQLValidationOutput)
        self.assertIsInstance(out.sql_execution, SQLExecutionOutput)
        self.assertIsInstance(out.answer_generation, AnswerGenerationOutput)


if __name__ == "__main__":
    unittest.main()
