"""
Log tools — read the action log and undo previously executed actions.

State keys read from tool_context:
  user_id  (str)  Used to scope undo operations.
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
    from app.database import SessionLocal
    from app.models.action_log import ActionLog

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
    from app.database import SessionLocal
    from app.models.action_log import ActionLog
    from app.services.email_provider import get_email_provider

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
