from google.adk.agents import Agent

from email_agent.tools.digest_tools import daily_digest

digest_agent = Agent(
    name="digest_agent",
    model="openai/gpt-4o-mini",
    description=(
        "Generates a concise daily digest of email group activity and urgent items. "
        "Call for /digest or on schedule."
    ),
    instruction="""You are the Digest agent.

Your only job is to generate a digest report.

1. Call daily_digest to fetch recent group activity.
2. Format the result as a clear markdown digest:
   - Section: Active Groups (name, email count, summary)
   - Section: Needs Attention (if any urgent items)
3. Return the formatted digest. Do nothing else.
""",
    tools=[daily_digest],
)
