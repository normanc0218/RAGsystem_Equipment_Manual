"""
Log tools — read the action log and undo previously executed actions.

State keys read from tool_context:
  user_id  (str)  Used to scope undo operations.

Undo workflow
─────────────
• "undo the last thing"  → undo_last_action()
• "undo the invoice email" → preview_undo(description) → show candidates
                            → user confirms → undo_action(log_id=<confirmed id>)

Never let the LLM guess a log_id from a list. Always use one of these entry
points so the id either comes from the DB directly or is confirmed by the user.
"""
from datetime import datetime

from google.adk.tools import ToolContext


def get_action_log(limit: int = 20, tool_context: ToolContext = None) -> list:
    """Retrieve recent action log entries from the database.

    Args:
        limit: Number of recent entries to return (max 100).
        tool_context: Injected by ADK — do not pass manually.

    Returns:
        List of log entries with id, timestamp, action, email_subject, status, undo_status.
    """
    from ..database import SessionLocal
    from ..models.action_log import ActionLog

    db = SessionLocal()
    try:
        logs = (
            db.query(ActionLog)
            .order_by(ActionLog.timestamp.desc())
            .limit(min(limit, 100))
            .all()
        )
        return [
            {
                "id": log.id,
                "timestamp": log.timestamp.isoformat(),
                "action": log.action,
                "email_subject": log.email_subject,
                "status": log.status,
                "undo_status": log.undo_status,
            }
            for log in logs
        ]
    finally:
        db.close()


def undo_action(log_id: int, tool_context: ToolContext = None) -> dict:
    """Reverse a previously logged archive or label action.

    Args:
        log_id: The action log ID to reverse (from get_action_log).
        tool_context: Injected by ADK — do not pass manually.

    Returns:
        Dict with success flag and details about what was undone.
    """
    from ..database import SessionLocal
    from ..models.action_log import ActionLog
    from ..services.email_provider import get_email_provider

    db = SessionLocal()
    try:
        log = db.query(ActionLog).filter(ActionLog.id == log_id).first()

        if not log:
            return {"success": False, "error": f"Action log #{log_id} not found"}
        if log.undo_status == "undone":
            return {"success": False, "error": f"Action #{log_id} has already been undone"}
        if log.status == "dry_run":
            return {"success": False, "error": "Cannot undo a dry-run action — nothing was written to Gmail"}

        provider = get_email_provider()
        if log.action == "archive":
            provider.unarchive_email(log.email_id)
        elif log.action == "label" and log.label:
            provider.remove_label(log.email_id, log.label)

        log.undo_status = "undone"
        log.undone_at = datetime.utcnow()
        db.commit()

        return {
            "success": True,
            "log_id": log_id,
            "action_reversed": log.action,
            "email_subject": log.email_subject,
        }
    except Exception as exc:
        log = db.query(ActionLog).filter(ActionLog.id == log_id).first()
        if log:
            log.undo_status = "undo_failed"
            db.commit()
        return {"success": False, "error": str(exc)}
    finally:
        db.close()


def undo_last_action(tool_context: ToolContext = None) -> dict:
    """Undo the most recent action that has not already been undone.

    Use this when the user says "undo that" or "undo the last action" —
    no log_id reasoning required.

    Args:
        tool_context: Injected by ADK — do not pass manually.

    Returns:
        Dict with success flag and details, same shape as undo_action.
    """
    from ..database import SessionLocal
    from ..models.action_log import ActionLog

    db = SessionLocal()
    try:
        log = (
            db.query(ActionLog)
            .filter(ActionLog.status != "dry_run")
            .filter(ActionLog.undo_status.is_(None))
            .order_by(ActionLog.timestamp.desc())
            .first()
        )
        if not log:
            return {"success": False, "error": "No undoable action found in the log"}
    finally:
        db.close()

    return undo_action(log_id=log.id, tool_context=tool_context)


def preview_undo(description: str, tool_context: ToolContext = None) -> dict:
    """Search the action log for entries matching a description and return candidates.

    Use this when the user wants to undo a specific action but hasn't provided
    a log_id. Present the returned candidates to the user, ask them to confirm
    which entry to undo, then call undo_action(log_id=<confirmed id>).

    Never call undo_action with a log_id you inferred yourself — always get
    explicit user confirmation from the candidates this function returns.

    Args:
        description: Free-text description of the action to undo, e.g.
                     "archive of the invoice email" or "newsletter label".
        tool_context: Injected by ADK — do not pass manually.

    Returns:
        Dict with a 'candidates' list. Each candidate has id, action,
        email_subject, timestamp, and status. Empty list means no match.
    """
    from ..database import SessionLocal
    from ..models.action_log import ActionLog

    keywords = [w.lower() for w in description.split() if len(w) > 2]

    db = SessionLocal()
    try:
        rows = (
            db.query(ActionLog)
            .filter(ActionLog.status != "dry_run")
            .filter(ActionLog.undo_status.is_(None))
            .order_by(ActionLog.timestamp.desc())
            .limit(50)
            .all()
        )

        def score(row: ActionLog) -> int:
            haystack = f"{row.action} {row.email_subject or ''}".lower()
            return sum(1 for kw in keywords if kw in haystack)

        candidates = [r for r in rows if score(r) > 0]
        candidates.sort(key=score, reverse=True)
        candidates = candidates[:5]

        return {
            "candidates": [
                {
                    "log_id": r.id,
                    "action": r.action,
                    "email_subject": r.email_subject,
                    "timestamp": r.timestamp.isoformat(),
                    "status": r.status,
                }
                for r in candidates
            ],
            "instruction": (
                "Show these candidates to the user and ask which log_id to undo. "
                "Only call undo_action after the user explicitly confirms a log_id."
            ),
        }
    finally:
        db.close()
