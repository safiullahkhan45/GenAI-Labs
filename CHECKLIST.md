# Production Readiness Checklist

**Instructions:** Complete all sections below. Check the box when an item is implemented, and provide descriptions where requested. This checklist is a required deliverable.

---

## Approach

Describe how you approached this assignment and what key problems you identified and solved.

- [x] **System works correctly end-to-end**

**What were the main challenges you identified?**
```
1. Token counting was a stub — _chat() never updated _stats, causing all token
   fields to report 0. This broke the efficiency evaluation contract.

2. SQL extraction was incomplete — _extract_sql() only handled raw JSON and raw
   SELECT text. LLMs almost always return markdown code fences (```sql...```),
   which caused extraction to silently fail or include backtick artefacts.

3. No schema context — generate_sql() passed {} as context. The LLM had no
   knowledge of the table name or column names, leading to invented column names
   and wrong table references.

4. SQL validation was a stub — SQLValidator.validate() always returned is_valid=True.
   A DELETE query would have been executed against the database.

5. Destructive requests were not caught — the pipeline would call the LLM for
   "delete all rows", waste tokens, and potentially execute the statement.

6. benchmark.py had a TypeError — result["status"] used dict syntax on a dataclass,
   causing the benchmark script to crash immediately.

7. No observability — zero logging, making it impossible to trace failures or
   measure per-stage latency in production.
```

**What was your approach?**
```
1. Token counting: read res.usage.prompt_tokens / completion_tokens / total_tokens
   from the OpenRouter ChatResult response and cast to int for the type contract.
   Increment llm_calls on every _chat() call.

2. SQL extraction: rewrote _extract_sql() to try three formats in order —
   markdown code fence, JSON object, raw SELECT scan — with trailing-text stripping.

3. Schema context: added _load_schema() to AnalyticsPipeline.__init__ which runs
   PRAGMA table_info once at startup and caches the result. The schema dict
   (table name + column names + types) is passed to generate_sql on every call,
   injected into the system prompt so the LLM uses exact column names.

4. SQL validation: implemented SQLValidator as an instance class with db_path.
   Checks: (a) not None/empty, (b) starts with SELECT, (c) no dangerous keywords
   via regex, (d) SQLite EXPLAIN to catch syntax errors and unknown columns/tables.

5. Destructive pre-check: added _is_destructive() regex check at the top of
   pipeline.run(). If triggered, returns a complete PipelineOutput with
   status="invalid_sql" and a non-None sql_validation.error — zero LLM calls.

6. benchmark.py fix: result["status"] -> result.status (dataclass attribute access).

7. Observability: added src/observability.py with structured logging (named loggers,
   timed_stage context manager, log_pipeline_result summary line). Every stage
   logs start/end/error with elapsed_ms. One INFO line per request with
   request_id, status, total_ms, llm_calls, total_tokens, and sql preview.
```

---

## Observability

- [x] **Logging**
  - Description: Structured logging via Python's `logging` module. Each module
    (`llm_client`, `pipeline`) has a named logger via `get_logger()`. Log format
    includes timestamp, level, logger name, and key=value fields for easy
    grep/parsing. DEBUG logs cover stage start/end with elapsed_ms. WARNING covers
    execution errors and blocked requests. INFO covers pipeline completion summaries.

- [x] **Metrics**
  - Description: Per-request metrics are captured in `PipelineOutput.timings`
    (sql_generation_ms, sql_validation_ms, sql_execution_ms, answer_generation_ms,
    total_ms) and `total_llm_stats` (llm_calls, prompt_tokens, completion_tokens,
    total_tokens). These are logged at INFO level on every completed request and
    returned in the output contract for downstream collection.

- [x] **Tracing**
  - Description: A `request_id` (UUID-derived, 8 chars) is generated at the start
    of every `pipeline.run()` call if not supplied by the caller. It is propagated
    through all log lines for that request, enabling correlation across stages in
    any log aggregation system (e.g. CloudWatch, Datadog, Grafana Loki).

---

## Validation & Quality Assurance

- [x] **SQL validation**
  - Description: Four-layer validation in `SQLValidator.validate()`:
    (1) None/empty check, (2) SELECT-only enforcement via startswith check,
    (3) dangerous keyword regex (DELETE/DROP/INSERT/UPDATE/TRUNCATE/ALTER/CREATE/
    REPLACE/ATTACH/DETACH/PRAGMA/VACUUM), (4) SQLite EXPLAIN bytecode compilation
    which validates full syntax, table existence, and column existence against the
    real database. Each rejection includes a descriptive error message.

- [x] **Answer quality**
  - Description: Schema-aware prompt ensures the LLM uses correct column names,
    improving first-try SQL accuracy. System prompt instructs the LLM to return
    {"sql": null} for unanswerable questions, which the pipeline handles as
    status="unanswerable" with a "cannot answer" message. Answer generation
    uses a concise analytics prompt with rows capped at 30 to keep completions focused.

- [x] **Result consistency**
  - Description: All four stage outputs are always populated regardless of failure
    path. Destructive rejections and unanswerable paths return zero-filled stats
    structs rather than None, preserving the output contract for automated evaluation.

- [x] **Error handling**
  - Description: Every stage is wrapped in try/except. LLM call failures set
    `error` on the stage output and return a graceful answer. SQL execution errors
    log a WARNING and return empty rows. The pipeline never raises — it always
    returns a PipelineOutput with an appropriate status.

---

## Maintainability

- [x] **Code organization**
  - Description: Single-responsibility modules: `observability.py` for logging,
    `llm_client.py` for LLM interaction, `pipeline.py` for orchestration.
    The destructive pre-check, SQL validator, SQL executor, and pipeline are
    cleanly separated. Schema loading is isolated to `_load_schema()`.

- [x] **Configuration**
  - Description: Model is configurable via `OPENROUTER_MODEL` env var (default:
    `openai/gpt-4o-mini`). DB path is a parameter to `AnalyticsPipeline.__init__`.
    API key via `OPENROUTER_API_KEY`. No hardcoded values in hot paths.

- [x] **Error handling**
  - Description: Errors are captured as strings in stage output `error` fields
    rather than raised, following the existing contract. All public methods return
    typed output objects — no unhandled exceptions escape the pipeline.

- [x] **Documentation**
  - Description: IMPLEMENTATION_PLAN.md covers full architecture and design decisions.
    SOLUTION_NOTES.md covers before/after analysis with benchmark numbers.
    Inline comments explain non-obvious decisions (e.g. why EXPLAIN not EXPLAIN
    QUERY PLAN, why pre-check over LLM refusal for destructive requests).

---

## LLM Efficiency

- [x] **Token usage optimization**
  - Description: Schema-aware prompts reduce retries by giving the LLM exact column
    names on the first call. Destructive pre-check eliminates the LLM entirely for
    modification requests (saves ~400 tokens per blocked call). Answer generation
    caps rows at 30 to limit prompt size. max_tokens set conservatively (300 for SQL,
    220 for answers).

- [x] **Efficient LLM requests**
  - Description: Schema loaded once at init, reused across all requests — no
    per-request DB queries. Exactly 2 LLM calls per successful request, 1 for
    unanswerable (answer is a static string), 0 for destructive requests.
    temperature=0.0 for SQL generation ensures deterministic output, reducing
    variance and the need for retries.

---

## Testing

- [x] **Unit tests**
  - Description: `tests/test_unit.py` — 33 tests, no API key required, runs in
    ~10ms. Covers SQL extraction (all 3 formats + edge cases), SQL validation
    (all reject/accept paths), destructive pre-check (9 cases), and PipelineOutput
    contract shape verification.

- [x] **Integration tests**
  - Description: `tests/test_public.py` (unchanged per hard requirement) — 5 tests
    against a live OpenRouter API and real SQLite DB. Covers: answerable query
    success path, unanswerable query handling, destructive SQL rejection, timings
    presence, and full output contract compatibility.

- [x] **Performance tests**
  - Description: `scripts/benchmark.py` — runs the full prompt set N times and
    reports avg, p50, p95 latency and success rate. Bug fixed: result["status"]
    -> result.status.

- [x] **Edge case coverage**
  - Description: Unit tests cover None SQL, empty string, DELETE/DROP/INSERT/UPDATE,
    PRAGMA, markdown code fences (with and without language tag), JSON null response,
    raw SELECT with trailing prose, and all destructive keyword patterns.

---

## Optional: Multi-Turn Conversation Support

Not implemented in this submission.

Proposed architecture if implemented:
- A `ConversationSession` class holding `history: list[Turn]` where each Turn
  stores {question, sql, rows, answer}.
- `pipeline.run(question, session=session)` injects the last Turn's context into
  the SQL generation prompt when a session is provided.
- Intent detection to distinguish follow-ups ("what about males?") from new
  standalone questions, using a lightweight regex heuristic or small LLM call.

---

## Production Readiness Summary

**What makes your solution production-ready?**
```
1. Never crashes — all exceptions are caught and returned as structured error outputs.
2. Request tracing — every log line carries a request_id for debugging.
3. Deterministic security — destructive requests blocked before any LLM call
   via regex pre-check, with a second layer of SQL keyword validation.
4. Schema-grounded generation — LLM receives the exact table schema, eliminating
   invented column names that silently fail at execution.
5. Validated SQL — EXPLAIN catches syntax errors and unknown columns before execution.
6. Token accounting — all token fields correctly populated for efficiency tracking.
7. Observable — structured logs at every stage, one summary line per request.
8. Typed contracts — all stage outputs always populated, enabling automated grading.
```

**Key improvements over baseline:**
```
- Token counting: always 0 -> real values from res.usage
- SQL extraction: fails on markdown -> handles all 3 LLM output formats
- SQL generation: no schema context -> full schema in system prompt
- SQL validation: always valid stub -> 4-layer validation with EXPLAIN
- Destructive requests: hit LLM or executed -> blocked at pre-check (0 tokens)
- Observability: none -> structured logs with request_id tracing
- Benchmark script: crashed (TypeError) -> fixed
- Unit tests: 0 -> 33 tests, no API key required
```

**Known limitations or future work:**
```
- No response caching — identical questions make duplicate LLM calls.
- No retry logic — a transient API error returns status="error" immediately.
- Multi-turn conversation not implemented (optional feature).
- EXPLAIN validation requires the DB to exist; if DB is missing the EXPLAIN step
  is skipped gracefully but schema errors won't be caught until execution time.
- Token counts depend on OpenRouter returning res.usage; if a provider omits it,
  counts fall back to 0 for that call.
```

---

## Benchmark Results

**Baseline (reference from README):**
- Average latency: `~2900 ms`
- p50 latency: `~2500 ms`
- p95 latency: `~4700 ms`
- Average tokens/request: `~600`

**My solution:**
- Average latency: `5822 ms`
- p50 latency: `5870 ms`
- p95 latency: `7555 ms`
- Success rate: `97.22 %`

**LLM efficiency:**
- Average tokens per request: `~700` (real counts from res.usage, was always 0 in baseline)
- Average LLM calls per request: `2 (success) / 1 (unanswerable) / 0 (destructive)`

---

**Completed by:** Safiullah Khan
**Date:** 2026-04-06
**Time spent:** ~5 hours