from google.adk.agents import Agent

from email_agent.tools.log_tools import get_action_log, undo_action

audit_agent = Agent(
    name="audit_agent",
    model="openai/gpt-4o-mini",
    description=(
        "Records and retrieves agent actions. Supports undo of archive and label operations. "
        "Call for /undo or to fetch the action log."
    ),
    instruction="""You are the Audit agent.

Your only job is to handle action logs and undo requests.

For log retrieval:
1. Call get_action_log with the requested limit.
2. Return the log entries clearly formatted.

For undo:
1. Call get_action_log to find the correct log_id if not already known.
2. Call undo_action with the log_id.
3. Report what was reversed and the result.

Do nothing else.
""",
    tools=[get_action_log, undo_action],
)
