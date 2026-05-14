"""
Token usage and cost metrics for OpenAI API calls.

Wraps chat completions and embeddings calls to capture usage automatically.
Call metrics.reset() at the start of each batch run, then metrics.log_summary()
at the end to print a full cost breakdown to the server log.

Pricing (as of May 2026):
  text-embedding-3-small : $0.020  / 1M tokens
  gpt-4o-mini input      : $0.150  / 1M tokens
  gpt-4o-mini output     : $0.600  / 1M tokens
"""
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_METRICS_FILE = Path(__file__).parent.parent.parent.parent / "metrics_runs.json"

logger = logging.getLogger(__name__)

# ── Pricing table (dollars per token) ────────────────────────────────────────
_PRICE: dict[str, dict[str, float]] = {
    "text-embedding-3-small": {"input": 0.020 / 1_000_000},
    "gpt-4o-mini":            {"input": 0.150 / 1_000_000, "output": 0.600 / 1_000_000},
    "gpt-4o":                 {"input": 2.50  / 1_000_000, "output": 10.00 / 1_000_000},
}


@dataclass
class _CallRecord:
    operation: str
    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0


class MetricsTracker:
    def __init__(self):
        self._records: dict[str, _CallRecord] = {}
        self.last_run: dict = {}   # snapshot saved after each log_summary()

    def reset(self):
        self._records.clear()

    def _record(self, operation: str, model: str, input_tokens: int,
                output_tokens: int, elapsed_s: float):
        key = f"{operation}:{model}"
        if key not in self._records:
            self._records[key] = _CallRecord(operation=operation, model=model)
        r = self._records[key]
        r.calls += 1
        r.input_tokens += input_tokens
        r.output_tokens += output_tokens
        r.elapsed_s += elapsed_s

        pricing = _PRICE.get(model, {})
        call_cost = (input_tokens * pricing.get("input", 0)
                     + output_tokens * pricing.get("output", 0))
        r.cost_usd += call_cost

        # Trace each direct OpenAI SDK call to GCP Cloud Logging
        try:
            from .cloud_logging_service import log_llm_call
            log_llm_call(
                model=model,
                agent_name=operation,
                tokens_in=input_tokens,
                tokens_out=output_tokens,
                cost_usd=call_cost,
                latency_s=elapsed_s,
                success=True,
            )
        except Exception:
            pass

    # ── Public wrappers ───────────────────────────────────────────────────────

    def chat(self, client: Any, operation: str, **kwargs) -> Any:
        """Wrap client.chat.completions.create() and record usage."""
        t0 = time.perf_counter()
        resp = client.chat.completions.create(**kwargs)
        elapsed = time.perf_counter() - t0
        usage = resp.usage
        self._record(
            operation=operation,
            model=kwargs.get("model", "unknown"),
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            elapsed_s=elapsed,
        )
        return resp

    def embed(self, client: Any, operation: str, **kwargs) -> Any:
        """Wrap client.embeddings.create() and record usage."""
        t0 = time.perf_counter()
        resp = client.embeddings.create(**kwargs)
        elapsed = time.perf_counter() - t0
        usage = resp.usage
        self._record(
            operation=operation,
            model=kwargs.get("model", "text-embedding-3-small"),
            input_tokens=usage.total_tokens if usage else 0,
            output_tokens=0,
            elapsed_s=elapsed,
        )
        return resp

    def log_summary(self, label: str = "run", pipeline_stats: dict | None = None):
        """Print a formatted cost breakdown to the server log and write to GCP."""
        if not self._records:
            logger.info("[metrics] %s — no API calls recorded", label)
            return

        total_calls = sum(r.calls for r in self._records.values())
        total_cost = sum(r.cost_usd for r in self._records.values())
        total_time = sum(r.elapsed_s for r in self._records.values())
        total_tokens = sum(r.input_tokens + r.output_tokens for r in self._records.values())

        lines = [f"[metrics] ── {label} ──────────────────────────"]
        for r in sorted(self._records.values(), key=lambda x: x.cost_usd, reverse=True):
            tok = f"{r.input_tokens:,} in"
            if r.output_tokens:
                tok += f" + {r.output_tokens:,} out"
            lines.append(
                f"  {r.operation:<22} {r.calls:>3} call{'s' if r.calls != 1 else ' '} │ "
                f"{tok:<28} │ ${r.cost_usd:.6f} │ {r.elapsed_s:.2f}s"
            )
        lines.append(
            f"  {'TOTAL':<22} {total_calls:>3} calls │ "
            f"{'':<28} │ ${total_cost:.6f} │ {total_time:.2f}s"
        )
        logger.info("\n".join(lines))
        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "label": label,
            "operations": self.get_summary(),
        }
        self.last_run = snapshot
        try:
            runs = json.loads(_METRICS_FILE.read_text()) if _METRICS_FILE.exists() else []
            runs.append(snapshot)
            _METRICS_FILE.write_text(json.dumps(runs, indent=2))
        except Exception as exc:
            logger.warning("Failed to persist metrics snapshot: %s", exc)

        if pipeline_stats:
            try:
                from .cloud_logging_service import log_pipeline_run
                log_pipeline_run(
                    label=label,
                    processed=pipeline_stats.get("processed", 0),
                    groups=pipeline_stats.get("groups", 0),
                    archived=pipeline_stats.get("archived", 0),
                    total_cost_usd=total_cost,
                    total_tokens=total_tokens,
                    elapsed_s=total_time,
                )
            except Exception:
                pass

    def get_summary(self) -> dict:
        """Return summary dict for programmatic use."""
        return {
            key: {
                "calls": r.calls,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cost_usd": round(r.cost_usd, 6),
                "elapsed_s": round(r.elapsed_s, 3),
            }
            for key, r in self._records.items()
        }


# Singleton — import and use directly
metrics = MetricsTracker()
