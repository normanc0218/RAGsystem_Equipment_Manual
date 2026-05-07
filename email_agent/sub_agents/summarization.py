from google.adk.agents import Agent

from email_agent.tools.project_tools import summarize_group, summarize_groups

summarization_agent = Agent(
    name="summarization_agent",
    model="openai/gpt-4o-mini",
    description=(
        "Generates or refreshes human-friendly summaries for email groups. "
        "Call after inbox processing to keep group summaries up to date."
    ),
    instruction="""You are the Summarization agent.

Your only job is to generate group summaries.

1. Call summarize_groups to refresh summaries for all groups that were touched.
   - Pass the list of group_ids provided by the master agent.
   - If no group_ids are provided, summarize all groups.
2. Return the summary text for each group.
3. Do nothing else.
""",
    tools=[summarize_groups, summarize_group],
)
