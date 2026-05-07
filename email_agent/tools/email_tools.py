"""
Email tools — read and write actions against the Gmail inbox.

State keys read from tool_context:
  user_id  (str)  Slack user ID, used to attribute action log entries.
  dry_run  (bool) When True, skips all Gmail write calls.
"""
import os

from google.adk.tools import ToolContext


def get_emails(max_results: int = 200, random_sample: bool = True, tool_context: ToolContext = None) -> list:
    """Fetch emails from the mailbox (excludes spam, trash, promotions).

    Args:
        max_results: Number of emails to fetch (default 200, max 500).
        random_sample: When True, fetches a larger pool and picks randomly for diversity.
        tool_context: Injected by ADK — do not pass manually.

    Returns:
        List of dicts with id, thread_id, subject, from, date, snippet.
    """
    from app.services.email_provider import get_email_provider

    provider = get_email_provider()
    return provider.fetch_emails(max_results=min(max_results, 500), random_sample=random_sample)


def archive_email(
    email_id: str,
    email_subject: str,
    reason: str,
    tool_context: ToolContext = None,
) -> dict:
    """Archive an email from the inbox. The action is logged and reversible via undo_action.

    Args:
        email_id: The unique Gmail message ID.
        email_subject: Subject line of the email (for the log).
        reason: Why this email is being archived.
        tool_context: Injected by ADK — do not pass manually.

    Returns:
        Dict with log_id, status, and dry_run flag.
    """
    from app.database import SessionLocal
    from app.models.action_log import ActionLog
    from app.services.email_provider import get_email_provider

    user_id = tool_context.state.get("user_id", "unknown") if tool_context else "unknown"
    dry_run = tool_context.state.get("dry_run", True) if tool_context else True

    provider = get_email_provider()
    status = "dry_run" if dry_run else "success"

    if not dry_run:
        provider.archive_email(email_id)

    db = SessionLocal()
    try:
        log = ActionLog(
            user=user_id,
            action="archive",
            email_id=email_id,
            email_subject=email_subject,
            status=status,
        )
        db.add(log)
        db.commit()
        db.refresh(log)
        return {"log_id": log.id, "status": status, "dry_run": dry_run, "reason": reason}
    finally:
        db.close()
