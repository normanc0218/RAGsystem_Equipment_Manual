from google.adk.agents import Agent

from email_agent.tools.email_tools import sync_gmail_labels, sync_gmail_labels_if_needed

mailbox_sync_agent = Agent(
    name="mailbox_sync_agent",
    model="openai/gpt-4o-mini",
    description=(
        "Syncs the user's existing Gmail labels into Firestore to seed the vector DB. "
        "Call this at the start of /organize to bootstrap or refresh group state."
    ),
    instruction="""You are the Mailbox Sync agent.

Your only job is to sync the user's Gmail labels into Firestore.

1. Call sync_gmail_labels_if_needed — it will skip if sync has already run recently.
2. Report how many labels were synced and which Firestore groups were created or updated.
3. Do nothing else.
""",
    tools=[sync_gmail_labels, sync_gmail_labels_if_needed],
)
