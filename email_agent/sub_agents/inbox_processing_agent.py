from google.adk.agents import Agent

from email_agent.tools.email_tools import archive_email, batch_process_emails

inbox_processing_agent = Agent(
    name="inbox_processing_agent",
    model="openai/gpt-4o-mini",
    description=(
        "Fetches all unprocessed inbox emails and groups or archives them. "
        "Handles the full classification loop internally — no per-email iteration needed."
    ),
    instruction="""You are the Inbox Processing agent.

Your only job is to process unprocessed emails.

1. Call batch_process_emails with the parameters given by the master agent.
   - This handles fetching, classification, grouping, and archiving entirely in Python.
   - You do NOT need to loop through individual emails.
2. Report the number of emails processed, groups created or merged, and emails archived.
3. Do nothing else.
""",
    tools=[batch_process_emails, archive_email],
)
