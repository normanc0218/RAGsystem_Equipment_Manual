from google.adk.tools import ToolContext

from ..services.firestore_service import list_groups


def daily_digest(tool_context: ToolContext = None) -> dict:
    """Return structured inbox data for the daily digest.

    Returns:
        Dict with group_count, total_emails, and groups list sorted by last activity.
    """
    groups = list_groups()
    if not groups:
        return {"group_count": 0, "total_emails": 0, "groups": []}

    sorted_groups = sorted(groups, key=lambda g: g.get("last_activity", ""), reverse=True)
    total_emails = sum(g.get("email_count", 0) for g in groups)

    return {
        "group_count": len(groups),
        "total_emails": total_emails,
        "groups": [
            {
                "name": g.get("name", "Unnamed"),
                "email_count": g.get("email_count", 0),
                "summary": (g.get("summary") or "")[:120],
                "last_activity": g.get("last_activity", ""),
            }
            for g in sorted_groups[:15]
        ],
    }
