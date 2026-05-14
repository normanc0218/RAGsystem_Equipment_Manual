"""
LiteLLM custom callback that ships every ADK agent model call to GCP Cloud Logging.

ADK uses LiteLLM internally for all Agent(model=...) calls. Registering this callback
in litellm.success_callback / litellm._async_success_callback captures those calls
without any changes to the agent code.
"""
import logging
from datetime import datetime

from litellm.integrations.custom_logger import CustomLogger

from .cloud_logging_service import log_llm_call

logger = logging.getLogger(__name__)


def _extract_agent_name(kwargs: dict) -> str:
    """Best-effort: read the calling agent name from ADK metadata in LiteLLM kwargs."""
    litellm_params = kwargs.get("litellm_params") or {}
    metadata = litellm_params.get("metadata") or {}
    return (
        metadata.get("agent_name")
        or metadata.get("caller_agent_id")
        or kwargs.get("model", "unknown")
    )


def _safe_cost(response_obj) -> float:
    try:
        import litellm as _litellm
        return float(_litellm.completion_cost(completion_response=response_obj))
    except Exception:
        return 0.0


def _build_payload(kwargs: dict, response_obj, start_time: datetime, end_time: datetime, success: bool) -> dict:
    usage = getattr(response_obj, "usage", None)
    tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
    latency_s = (end_time - start_time).total_seconds() if start_time and end_time else 0.0
    return {
        "model": kwargs.get("model", "unknown"),
        "agent_name": _extract_agent_name(kwargs),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": _safe_cost(response_obj) if success else 0.0,
        "latency_s": latency_s,
        "success": success,
    }


class GCPLiteLLMCallback(CustomLogger):

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            log_llm_call(**_build_payload(kwargs, response_obj, start_time, end_time, success=True))
        except Exception as exc:
            logger.debug("GCPLiteLLMCallback.log_success_event failed: %s", exc)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            log_llm_call(**_build_payload(kwargs, response_obj, start_time, end_time, success=True))
        except Exception as exc:
            logger.debug("GCPLiteLLMCallback.async_log_success_event failed: %s", exc)

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        try:
            log_llm_call(**_build_payload(kwargs, response_obj, start_time, end_time, success=False))
        except Exception as exc:
            logger.debug("GCPLiteLLMCallback.log_failure_event failed: %s", exc)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        try:
            log_llm_call(**_build_payload(kwargs, response_obj, start_time, end_time, success=False))
        except Exception as exc:
            logger.debug("GCPLiteLLMCallback.async_log_failure_event failed: %s", exc)
