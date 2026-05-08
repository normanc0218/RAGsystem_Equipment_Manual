from google.adk.agents import Agent

from ..tools.email_tools import archive_email, batch_process_emails

inbox_processing_agent = Agent(
    name="inbox_processing_agent",
    model="openai/gpt-4o-mini",
    description=(
        "Fetches all unprocessed inbox emails and groups or archives them. "
        "Handles the full classification loop internally — no per-email iteration needed."
    ),
    instruction="""You are the Inbox Processing agent.

1. Call batch_process_emails with the parameters given by the master agent.
   - This handles fetching, classification, grouping, and archiving entirely in Python.
   - You do NOT need to loop through individual emails.
2. Return a structured result with ALL of the following fields exactly:
   - fetched, already_processed, processed, grouped, archived
   - groups: full dict of group name → {emails, archived, summary}
   - needs_attention: list of {subject, from, group, reason}
   Pass the full result back — the master agent uses it to compile the final report.
""",
    tools=[batch_process_emails, archive_email],
)
