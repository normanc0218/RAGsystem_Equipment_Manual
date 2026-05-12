from google.adk.agents import Agent

from ..tools.digest_tools import daily_digest

digest_agent = Agent(
    name="digest_agent",
    model="openai/gpt-4o-mini",
    description=(
        "Generates a concise daily digest of email group activity and urgent items. "
        "Call for /digest or on schedule."
    ),
    instruction="""You are the Digest agent.

1. Call daily_digest to fetch inbox data.
2. Return the tool result as-is — do not reformat, summarize, or add any text.
   The Slack UI will render it. Any text you add will break the formatting.
""",
    tools=[daily_digest],
)
