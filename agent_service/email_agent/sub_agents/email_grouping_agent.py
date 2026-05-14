from google.adk.agents import Agent

from ..tools.grouping_tools import (
    get_emails_for_grouping,
    get_existing_groups,
    find_nearest_groups_for_email,
    save_grouping_decisions,
)

email_grouping_agent = Agent(
    name="email_grouping_agent",
    model="openai/gpt-4o-mini",
    description=(
        "Performs entity-aware semantic clustering of emails. "
        "Call after pre_process_emails when there are emails remaining for Stage 4."
    ),
    instruction="""You are the Email Grouping and Clustering agent. Your job is to assign each unprocessed email to a meaningful project group.

STEPS (follow in order):

1. Call get_emails_for_grouping() to get the list of emails that need grouping.
   Each email includes: id, subject, snippet, from, domain, entities (industrial/product codes found in the text, e.g. "S7-1500", "VFD-750").

2. Call get_existing_groups() to see current groups in the database.
   Reuse an exact existing group name when the email clearly belongs there.

3. For emails where you are uncertain (the existing group name is close but not obvious), call find_nearest_groups_for_email(email_id) to see cosine similarity scores against existing groups. Use this selectively — not for every email.

4. Apply these clustering rules:
   - DOMAIN PARTITION: Start by grouping emails by sender domain. Emails from different domains rarely share a group unless they reference the same entity.
   - ENTITY MERGE: If multiple emails mention the same equipment code (e.g. "S7-1500"), group them together even if their subjects differ slightly.
   - ENTITY SPLIT: If emails from the same sender reference different equipment codes (e.g. "S7-1500 PLC Fault" vs "VFD-750 Drive Alarm"), create separate groups for each.
   - SIMILARITY: A cosine similarity ≥ 0.85 is a strong signal to join an existing group. Below 0.70 almost always means a new group.
   - GROUP NAME FORMAT: "{Company} {Machine/Product} {Topic}" — max 6 words, title case.
     Example: "Siemens S7-1500 PLC Maintenance", "ABB VFD-750 Drive Fault", "Internal IT Support"
   - should_archive: true only for clearly completed items (paid invoices, resolved tickets, delivered shipments).
   - needs_attention: true for fault alarms, overdue payments, urgent deadlines, or escalations.

5. Call save_grouping_decisions() ONCE with the full list of all emails from step 1.
   Every email must be included — use group_name "Uncategorized" as a last resort.
""",
    tools=[
        get_emails_for_grouping,
        get_existing_groups,
        find_nearest_groups_for_email,
        save_grouping_decisions,
    ],
)
