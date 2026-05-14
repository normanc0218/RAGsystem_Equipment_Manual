from typing import AsyncGenerator

from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from ..tools.email_tools import pre_process_emails, finalize_email_processing
from .email_grouping_agent import email_grouping_agent


class _StateAdapter:
    """Bridges InvocationContext.session.state to the tool_context.state interface."""
    def __init__(self, ctx: InvocationContext):
        self.state = ctx.session.state


class _PreProcessAgent(BaseAgent):
    """Runs Stages 1-3 as pure Python — zero LLM calls, zero tokens, no yielded events."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        pre_process_emails(tool_context=_StateAdapter(ctx))
        return  # yield nothing — purely internal, root agent doesn't need to see this
        yield  # make Python treat this as an async generator


class _ConditionalGroupingAgent(BaseAgent):
    """Runs email_grouping_agent only when emails_to_cluster is non-empty — zero tokens otherwise."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        if not ctx.session.state.get("emails_to_cluster"):
            return  # yield nothing — skip is an internal detail
            yield
        async for event in self.sub_agents[0].run_async(ctx):
            yield event


class _FinalizeAgent(BaseAgent):
    """Runs Steps 5-6 as pure Python — yields clean JSON for the root agent to compile the report."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        import json
        result = finalize_email_processing(tool_context=_StateAdapter(ctx))
        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=json.dumps(result))],
            ),
        )


inbox_processing_agent = SequentialAgent(
    name="inbox_processing_agent",
    description=(
        "Processes inbox emails in guaranteed order: "
        "Stages 1-3 → Stage 4 entity-aware clustering (skipped if none remain) → finalize."
    ),
    sub_agents=[
        _PreProcessAgent(
            name="pre_process_agent",
            description="Runs Stages 1-3: user-labelled emails, template detection, thread grouping.",
        ),
        _ConditionalGroupingAgent(
            name="conditional_grouping_agent",
            description="Runs email_grouping_agent only when emails_to_cluster is non-empty.",
            sub_agents=[email_grouping_agent],
        ),
        _FinalizeAgent(
            name="finalize_agent",
            description="Saves group assignments to Firestore, applies labels, archives, and summarizes.",
        ),
    ],
)
