from google.adk.tools import ToolContext

from ..services.firestore_service import list_groups


def daily_digest(tool_context: ToolContext = None) -> dict:
    """Generate a daily digest of project groups and recent activity."""
    groups = list_groups()
    if not groups:
        return {"digest": "No email groups have been created yet."}

    sorted_groups = sorted(groups, key=lambda g: g.get("last_activity", ""), reverse=True)
    lines = []
    for group in sorted_groups[:10]:
        name = group.get("name", "Unnamed Group")
        email_count = group.get("email_count", 0)
        summary = group.get("summary") or "No summary yet."
        lines.append(f"*{name}* — {email_count} emails\n{summary}")

    digest_text = "\n\n".join(lines)
    return {
        "group_count": len(groups),
        "digest": digest_text,
    }
