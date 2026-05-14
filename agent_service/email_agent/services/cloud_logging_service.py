"""
Write-ahead log + background queue for Google Cloud Logging.

log_llm_call() and log_pipeline_run() are non-blocking:
  1. Write to local JSONL immediately (microseconds, no network)
  2. Put entry on an in-process queue.Queue
  3. Background daemon thread drains the queue and ships to GCP

Falls back silently if GOOGLE_APPLICATION_CREDENTIALS is not set or GCP is unreachable.
Local JSONL files provide durability: logs/llm_calls.jsonl, logs/pipeline_runs.jsonl
"""
import json
import logging
import os
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_LOG_DIR       = Path(__file__).parent.parent.parent.parent / "logs"
_LLM_LOG_FILE  = _LOG_DIR / "llm_calls.jsonl"
_PIPE_LOG_FILE = _LOG_DIR / "pipeline_runs.jsonl"
_GCP_PROJECT   = os.getenv("GOOGLE_CLOUD_PROJECT", "email-agent-dev-b6b5a")

_queue: queue.Queue = queue.Queue(maxsize=1000)


def _gcp_worker() -> None:
    """Daemon thread: drains _queue and ships entries to GCP Cloud Logging."""
    llm_log = pipeline_log = None
    while True:
        entry = _queue.get()
        if llm_log is None and os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            try:
                from google.cloud import logging as gcp_logging
                client = gcp_logging.Client(project=_GCP_PROJECT)
                llm_log      = client.logger("email-agent-llm")
                pipeline_log = client.logger("email-agent-pipeline")
                logger.info("GCP Logging initialised (project=%s)", _GCP_PROJECT)
            except Exception as exc:
                logger.warning("GCP Logging init failed: %s", exc)
        try:
            kind = entry.get("type")
            if kind == "llm_call" and llm_log:
                llm_log.log_struct(
                    entry,
                    severity="INFO" if entry.get("success") else "WARNING",
                )
            elif kind == "pipeline_run" and pipeline_log:
                pipeline_log.log_struct(entry, severity="INFO")
        except Exception as exc:
            logger.debug("GCP log_struct failed: %s", exc)
        finally:
            _queue.task_done()


threading.Thread(target=_gcp_worker, daemon=True, name="gcp-log-worker").start()


def _write_local(path: Path, entry: dict) -> None:
    try:
        _LOG_DIR.mkdir(exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        logger.debug("local log write failed: %s", exc)


def _enqueue(entry: dict) -> None:
    try:
        _queue.put_nowait(entry)
    except queue.Full:
        logger.debug("GCP log queue full, entry dropped")


def log_llm_call(
    model: str,
    agent_name: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    latency_s: float,
    success: bool,
) -> None:
    entry = {
        "type": "llm_call",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "agent_name": agent_name,
        "tokens_in": int(tokens_in),
        "tokens_out": int(tokens_out),
        "cost_usd": round(float(cost_usd), 8),
        "latency_s": round(float(latency_s), 3),
        "success": bool(success),
    }
    _write_local(_LLM_LOG_FILE, entry)
    _enqueue(entry)


def log_pipeline_run(
    label: str,
    processed: int,
    groups: int,
    archived: int,
    total_cost_usd: float,
    total_tokens: int,
    elapsed_s: float,
) -> None:
    entry = {
        "type": "pipeline_run",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "processed": int(processed),
        "groups": int(groups),
        "archived": int(archived),
        "total_cost_usd": round(float(total_cost_usd), 8),
        "total_tokens": int(total_tokens),
        "elapsed_s": round(float(elapsed_s), 3),
    }
    _write_local(_PIPE_LOG_FILE, entry)
    _enqueue(entry)
