"""
Inbox query tools — read-only views over Firestore for answering user questions
about their organised email (group counts, summaries, email lists, etc.).
"""
import logging

logger = logging.getLogger(__name__)


def get_inbox_stats() -> dict:
    """Return a full snapshot of the user's organised inbox from Firestore.

    Covers:
    - Total number of groups
    - Total processed emails
    - Per-group: name, email count, summary, last activity

    Returns:
        Dict with total_groups, total_emails, and a groups list.
    """
    from ..services.firestore_service import list_groups, _db, SUMMARIES

    groups = list_groups()
    total_emails = sum(g.get("email_count", 0) for g in groups)

    return {
        "total_groups": len(groups),
        "total_emails": total_emails,
        "groups": [
            {
                "name": g.get("name", ""),
                "email_count": g.get("email_count", 0),
                "summary": g.get("summary", ""),
                "last_activity": g.get("last_activity", ""),
                "source": g.get("source", "agent"),
            }
            for g in sorted(groups, key=lambda x: x.get("email_count", 0), reverse=True)
        ],
    }


def get_group_emails(group_name: str) -> dict:
    """Return emails belonging to a specific group, looked up by name.

    Args:
        group_name: The group name to look up (case-insensitive partial match).

    Returns:
        Dict with group info and list of emails (subject, sender, date, snippet).
    """
    from ..services.firestore_service import list_groups, _db, SUMMARIES
    from google.cloud.firestore_v1.base_query import FieldFilter

    groups = list_groups()
    name_lower = group_name.lower()
    matched = [g for g in groups if name_lower in g.get("name", "").lower()]

    if not matched:
        return {"error": f"No group found matching '{group_name}'", "groups_available": [g["name"] for g in groups]}

    group = matched[0]
    group_id = group["group_id"]

    docs = (
        _db().collection(SUMMARIES)
        .where(filter=FieldFilter("group_id", "==", group_id))
        .stream()
    )
    emails = [
        {
            "subject": d.get("subject", "(no subject)"),
            "sender": d.get("sender", ""),
            "date": d.get("date", ""),
            "snippet": d.get("snippet", ""),
        }
        for doc in docs
        for d in [doc.to_dict()]
    ]

    return {
        "group_name": group.get("name"),
        "email_count": len(emails),
        "summary": group.get("summary", ""),
        "emails": emails,
    }
