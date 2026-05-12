# Observability & Metrics

This document covers what metrics are tracked, how they are calculated, and what a production AI agent system should monitor.

---

## Current Implementation

### How Metrics Are Calculated

All LLM calls pass through `MetricsTracker` in [`metrics_service.py`](../agent_service/email_agent/services/metrics_service.py).

**Token counts** are read from the OpenAI response `usage` object after every call:

```python
usage.prompt_tokens      # input tokens (chat)
usage.completion_tokens  # output tokens (chat)
usage.total_tokens       # total tokens (embeddings)
```

**Cost** is calculated by multiplying tokens by the hardcoded price table:

| Model | Input | Output |
|---|---|---|
| `gpt-4o-mini` | $0.150 / 1M tokens | $0.600 / 1M tokens |
| `text-embedding-3-small` | $0.020 / 1M tokens | — |
| `gpt-4o` | $2.50 / 1M tokens | $10.00 / 1M tokens |

**Latency** is wall-clock time using `time.perf_counter()` around each API call.

### What Is Tracked Per Run

Each `/organize` run records a snapshot under these operations:

| Operation Key | Model | Trigger |
|---|---|---|
| `classify` | `gpt-4o-mini` | Grouping all fetched emails in one call |
| `summarize` | `gpt-4o-mini` | Incremental group summary update |
| `embedding:single` | `text-embedding-3-small` | One call per email during vector grouping |

### How to View Metrics

After running `/organize` in Slack, open:

```
https://<your-codespace>-8000.app.github.dev/metrics
```

Example response:

```json
{
  "label": "batch_process_emails (20 emails, 5 groups)",
  "summary": {
    "total_calls": 3,
    "total_tokens": 4230,
    "total_cost_usd": 0.000412,
    "total_elapsed_s": 2.841
  },
  "operations": {
    "classify:gpt-4o-mini":              { "calls": 1, "input_tokens": 1240, "output_tokens": 180, "cost_usd": 0.000294, "elapsed_s": 1.12 },
    "summarize:gpt-4o-mini":             { "calls": 1, "input_tokens": 890,  "output_tokens": 210, "cost_usd": 0.000260, "elapsed_s": 0.98 },
    "embedding:single:text-embedding-3-small": { "calls": 18, "input_tokens": 2100, "output_tokens": 0, "cost_usd": 0.000042, "elapsed_s": 0.74 }
  }
}
```

Metrics are also printed to the server log after every run:

```
[metrics] ── batch_process_emails (20 emails, 5 groups) ──────────
  classify               1 call  │ 1,240 in + 180 out    │ $0.000294 │ 1.12s
  summarize              1 call  │ 890 in + 210 out      │ $0.000260 │ 0.98s
  embedding:single      18 calls │ 2,100 in              │ $0.000042 │ 0.74s
  TOTAL                 20 calls │                       │ $0.000596 │ 2.84s
```

---

## Production Metrics Framework

A production AI agent system should be monitored across four layers.

### Layer 1 — LLM Cost & Quality

These metrics directly affect your OpenAI bill and output reliability.

| Metric | How to Measure | Alert Threshold |
|---|---|---|
| Input tokens per operation | `usage.prompt_tokens` per call | >2× baseline |
| Output tokens per operation | `usage.completion_tokens` per call | >3× baseline |
| Cost per request (p50, p95) | Sum cost across all LLM calls per Slack command | >$0.05 per request |
| Output/input token ratio | `completion_tokens / prompt_tokens` | >1.0 (runaway generation) |
| Prompt cache hit rate | `usage.cached_tokens` (if using prompt caching) | <50% on repeated prompts |

### Layer 2 — Latency & Reliability

These metrics affect user-perceived performance and SLA.

| Metric | How to Measure | Alert Threshold |
|---|---|---|
| End-to-end request latency | Time from Slack command received to response sent | >30s |
| LLM latency vs total latency | LLM elapsed / total elapsed | >80% means no parallelism gains available |
| API error rate | Count of exceptions from OpenAI calls | >2% over 5 min |
| Retry count per request | Track retries in the OpenAI client | >1 retry on average |
| Gmail API batch latency | Time for `batch.execute()` | >5s for 20 emails |
| Firestore read latency | Time for `get_all()` | >2s for 20 docs |

### Layer 3 — Agent Behaviour

These metrics are specific to multi-agent systems and catch logic problems that unit tests miss.

| Metric | How to Measure | Why It Matters |
|---|---|---|
| Sub-agent routing distribution | Log which agent handles each intent | Detects misrouting (e.g. QUERY going to ORGANIZE) |
| Tool call count per session | Count ADK tool invocations | Runaway loops inflate cost silently |
| Session duration | Time from first to last tool call | Long sessions = state accumulation risk |
| Fallback rate | How often root agent can't route intent | Signals prompt needs updating |
| Groups created vs emails processed | `groups_created / emails_processed` | Low ratio = over-grouping, high = under-grouping |

### Layer 4 — Business Outcomes

These metrics measure whether the agent is delivering real value.

| Metric | How to Measure | Target |
|---|---|---|
| Emails processed per run | Count in `batch_process_emails` result | ≥15 of 20 requested |
| Label apply success rate | Successful Gmail label calls / attempted | >95% |
| Undo rate | `/undo` commands / `/organize` commands | <10% (proxy for mistake rate) |
| Digest opens | Slack link clicks on digest messages | Engagement signal |
| `/organize` runs per day per user | Count Slack commands | Adoption signal |

---

## Current Gaps (vs Production Standard)

| Gap | Impact | Recommended Fix |
|---|---|---|
| No per-request trace ID | Can't correlate a Slack command to its LLM calls in logs | Add `request_id = uuid4()` at Slack handler entry, pass through to metrics |
| Failed calls not recorded | Error rate is invisible | Wrap `metrics.chat()` / `metrics.embed()` in try/except and record failures |
| No latency histogram | Can't compute p95 — only sees single-run elapsed time | Store per-call durations in a list, compute percentiles |
| In-memory only | Metrics lost on server restart | Write `last_run` to SQLite alongside action logs |
| No agent routing metrics | Don't know which sub-agent handled each command | Log intent + sub-agent name in `slack.py` before `_run_agent()` |
| Prices hardcoded | Stale if OpenAI changes pricing | Pull from a config file or environment variable |

---

## Recommended Stack for Scale

When the system grows beyond a single user or Codespace:

| Concern | Recommended Tool | Notes |
|---|---|---|
| LLM tracing (prompt/response/tokens) | [Langfuse](https://langfuse.com) (open source) or [Arize Phoenix](https://phoenix.arize.com) | Drop-in OpenAI wrapper, self-hostable |
| Infrastructure metrics (CPU, memory, latency) | OpenTelemetry → Grafana | Standard for FastAPI apps |
| Cost alerting | OpenAI Usage Limits (platform.openai.com) | Set hard monthly spend limits |
| Log aggregation | Datadog / Loki + Grafana | Structured JSON logs from Python `logging` |
| Error tracking | Sentry | Captures exceptions with full stack trace and context |
