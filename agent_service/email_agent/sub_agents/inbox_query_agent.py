from google.adk.agents import Agent

from ..tools.inbox_query_tools import get_inbox_stats, get_group_emails

inbox_query_agent = Agent(
    name="inbox_query_agent",
    model="openai/gpt-4o-mini",
    description=(
        "Answers questions about the user's organised inbox using Firestore as the source of truth. "
        "Use for: how many groups, group names, email counts, group summaries, emails in a group."
    ),
    instruction="""You are the Inbox Query agent. Answer the user's question about their organised email.

Available tools:
- get_inbox_stats: overall snapshot — total groups, total emails, all group names/counts/summaries
- get_group_emails: emails inside a specific group by name

Rules:
- Always call get_inbox_stats first unless the question is clearly about a specific group.
- If the user asks about a specific group, call get_group_emails with the group name.
- Answer in plain conversational text using Slack mrkdwn (*bold* not **bold**).
- Be concise — one or two sentences per fact.
- Never call tools outside this list.
""",
    tools=[get_inbox_stats, get_group_emails],
)
