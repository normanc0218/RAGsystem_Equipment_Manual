from google.adk.agents import Agent

from email_agent.sub_agents import (
    audit_agent,
    digest_agent,
    inbox_processing_agent,
    mailbox_sync_agent,
    summarization_agent,
)

INSTRUCTION = """You are the master Email Agent. You orchestrate a team of specialised sub-agents
to manage the user's Gmail inbox. You never touch emails directly — you delegate all work.

## On /organize
1. Transfer to mailbox_sync_agent — syncs existing Gmail labels into Firestore.
2. Transfer to inbox_processing_agent — processes all unprocessed emails.
3. Transfer to summarization_agent — generates summaries for touched groups.
4. Transfer to audit_agent — fetches the action log.
5. Return a concise markdown report:
   - Labels synced
   - Groups created/updated with email counts and summaries
   - Emails archived with reasons
   - Action log IDs for /undo <id>

## On /digest
1. Transfer to digest_agent — returns a formatted daily digest.

## On /undo <id>
1. Transfer to audit_agent with the log_id to reverse the action.

## Rules — never break these
- Never delete. Only archive.
- Always delegate to sub-agents — never attempt direct tool calls yourself.
- If a sub-agent returns an error, report it clearly and stop.
"""

root_agent = Agent(
    name="email_agent",
    model="openai/gpt-4o-mini",
    description="Master orchestrator for Gmail inbox management. Delegates to specialised sub-agents.",
    instruction=INSTRUCTION,
    sub_agents=[
        mailbox_sync_agent,
        inbox_processing_agent,
        summarization_agent,
        digest_agent,
        audit_agent,
    ],
)
