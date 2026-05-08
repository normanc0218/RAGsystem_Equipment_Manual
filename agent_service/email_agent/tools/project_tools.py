"""
Project tools — manage email groups in Firestore and generate AI summaries.
"""
from google.adk.tools import ToolContext


def group_emails(
    project_name: str,
    email_ids: list[str],
    description: str = "",
    sender: str = "",
    thread_id: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Group related emails under a named project using three-layer vector clustering.

    Finds an existing matching group or creates a new one. Each email belongs to at most one group.

    Args:
        project_name: Short descriptive name for the project or topic (5 words max).
        email_ids: List of Gmail message IDs belonging to this group.
        description: One-line description of what this group represents.
        sender: Email address of the sender (used for structural matching).
        thread_id: Gmail thread ID (used for structural matching).
        tool_context: Injected by ADK — do not pass manually.

    Returns:
        Dict with group_id, name, email_count, and action (created | merged).
    """
    from ..services.grouping_service import find_or_create_group

    return find_or_create_group(
        project_name=project_name,
        email_ids=email_ids,
        description=description,
        thread_id=thread_id,
        sender=sender,
    )


def summarize_groups(group_ids: list[str], tool_context: ToolContext = None) -> dict:
    """Generate summaries for several groups as a separate summarization sub-agent."""
    summaries = []
    for group_id in group_ids:
        summary = summarize_group(group_id, tool_context=tool_context)
        summaries.append(summary)
    return {"summaries": summaries}


def summarize_group(group_id: str, tool_context: ToolContext = None) -> dict:
    """Generate an AI summary for a project group and save it to Firestore.

    Args:
        group_id: The Firestore group ID returned by group_emails.
        tool_context: Injected by ADK — do not pass manually.

    Returns:
        Dict with group_id, name, and the generated summary text.
    """
    from openai import OpenAI

    from ..services import firestore_service
    from ..services.email_provider import get_email_provider

    group = firestore_service.get_group(group_id)
    if not group:
        return {"error": f"Project group '{group_id}' not found in Firestore"}

    email_ids = set(group.get("email_ids") or [])
    all_emails = get_email_provider().fetch_emails(max_results=50, random_sample=False)
    group_emails = [e for e in all_emails if e["id"] in email_ids]

    if not group_emails:
        return {"error": "No emails found for this group — skipping summary"}

    lines = [
        f"- Subject: {e['subject']} | From: {e['from']} | Preview: {e['snippet']}"
        for e in group_emails
    ]
    prompt = (
        f"Project group: {group['name']}\n"
        f"Description: {group.get('description') or 'N/A'}\n\n"
        f"Emails:\n" + "\n".join(lines) + "\n\n"
        "Write a 2-3 sentence summary of this project group. Be concise and actionable."
    )

    response = OpenAI().chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Summarize these emails in 2-3 sentences. Be concise and actionable."},
            {"role": "user", "content": prompt},
        ],
    )
    summary = response.choices[0].message.content.strip()
    firestore_service.update_group(group_id, {"summary": summary})
    return {"group_id": group_id, "name": group["name"], "summary": summary}
