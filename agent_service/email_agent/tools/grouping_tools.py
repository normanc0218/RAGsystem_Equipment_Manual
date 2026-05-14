"""
Tools for the email grouping and clustering ADK agent.

Session state contract (keys written/read by these tools):
  emails_to_cluster      list[dict]  Written by pre_process_emails; read here.
  email_pre_cls          dict        Written by pre_process_emails; read by finalize.
  _all_emails            list[dict]  Written by pre_process_emails; read by finalize.
  _total_scanned         int         Written by pre_process_emails; read by finalize.
  _already_processed     int         Written by pre_process_emails; read by finalize.
  _grouping_embeddings   dict        Internal embedding cache: email_id → vector.
  grouping_assignments   dict        Written by save_grouping_decisions; read by finalize.
"""
import logging
import re

from google.adk.tools import ToolContext

logger = logging.getLogger(__name__)

# Matches common industrial/product codes: S7-1500, VFD-750, ET200SP, CPU-315, IM-153
_ENTITY_RE = re.compile(
    r'\b('
    r'[A-Z][A-Z0-9]{1,5}[-/][0-9A-Z]{2,10}'
    r'|[A-Z]{2,6}[-][0-9]{3,6}'
    r')\b'
)


def _sender_domain(sender: str) -> str:
    match = re.search(r"@([\w.\-]+)", sender)
    return match.group(1).lower() if match else "unknown"


def get_emails_for_grouping(tool_context: ToolContext) -> dict:
    """Retrieve emails staged for semantic clustering, enriched with domain and entity codes.

    Reads emails_to_cluster from session state (populated by pre_process_emails).
    Extracts sender domain and industrial entity codes (e.g. S7-1500, VFD-750) from
    each email's subject and snippet without any API calls.

    Args:
        tool_context: Injected by ADK — do not pass manually.

    Returns:
        Dict with count and list of emails: {id, subject, snippet, from, domain, entities}.
    """
    emails = tool_context.state.get("emails_to_cluster", [])
    enriched = []
    for email in emails:
        domain = _sender_domain(email.get("from", ""))
        text = f"{email.get('subject', '')} {email.get('snippet', '')}"
        entities = list(set(_ENTITY_RE.findall(text)))
        enriched.append({
            "id": email["id"],
            "subject": email.get("subject", ""),
            "snippet": email.get("snippet", "")[:200],
            "from": email.get("from", ""),
            "domain": domain,
            "entities": entities,
        })
    return {"count": len(enriched), "emails": enriched}


def get_existing_groups(tool_context: ToolContext = None) -> dict:
    """List all existing Firestore groups — use to find reusable group names.

    Args:
        tool_context: Injected by ADK — do not pass manually.

    Returns:
        Dict with count and list of {name, description, email_count}.
    """
    from ..services.firestore_service import list_group_details
    groups = list_group_details()
    return {
        "count": len(groups),
        "groups": [
            {
                "name": g["name"],
                "description": g.get("description", ""),
                "email_count": g.get("email_count", 0),
            }
            for g in groups
        ],
    }


def find_nearest_groups_for_email(email_id: str, tool_context: ToolContext = None) -> dict:
    """Vector-search the top-3 most similar Firestore groups for a given email.

    Computes an embedding for the email (or retrieves a cached one) and runs KNN.
    Use this for emails where the similarity band is ambiguous (0.70–0.88).

    Args:
        email_id: Must match an id present in emails_to_cluster session state.
        tool_context: Injected by ADK — do not pass manually.

    Returns:
        Dict with email_id and candidates: [{name, description, similarity, group_id}].
    """
    from ..services.embedding_service import get_embedding
    from ..services.firestore_service import find_nearest_group_top_k

    emails = tool_context.state.get("emails_to_cluster", []) if tool_context else []
    email = next((e for e in emails if e["id"] == email_id), None)
    if not email:
        return {"email_id": email_id, "error": "not found in session state", "candidates": []}

    emb_cache = tool_context.state.get("_grouping_embeddings", {}) if tool_context else {}
    if email_id not in emb_cache:
        text = f"{email.get('subject', '')} {email.get('snippet', '')}"
        emb_cache[email_id] = get_embedding(text)
        if tool_context:
            tool_context.state["_grouping_embeddings"] = emb_cache

    try:
        candidates = find_nearest_group_top_k(emb_cache[email_id], k=3)
    except Exception as exc:
        logger.warning("KNN search failed for %s: %s", email_id, exc)
        candidates = []

    return {"email_id": email_id, "candidates": candidates}


def save_grouping_decisions(decisions: list[dict], tool_context: ToolContext = None) -> dict:
    """Persist semantic clustering decisions to session state for finalize_email_processing.

    Call this once with the full list of all emails from get_emails_for_grouping() — every
    email must be covered.

    Args:
        decisions: List of dicts with keys:
            email_id (str), group_name (str), should_archive (bool),
            archive_reason (str), needs_attention (bool), attention_reason (str).
        tool_context: Injected by ADK — do not pass manually.

    Returns:
        Dict with saved count and list of any email IDs not yet covered.
    """
    if not tool_context:
        return {"error": "no tool_context available", "saved": 0}

    assignments = tool_context.state.get("grouping_assignments", {})
    for d in decisions:
        eid = d.get("email_id")
        if not eid:
            continue
        assignments[eid] = {
            "group_name": d.get("group_name", "Uncategorized"),
            "should_archive": bool(d.get("should_archive", False)),
            "archive_reason": d.get("archive_reason", ""),
            "needs_attention": bool(d.get("needs_attention", False)),
            "attention_reason": d.get("attention_reason", ""),
        }

    staged_ids = {e["id"] for e in tool_context.state.get("emails_to_cluster", [])}
    missing = staged_ids - set(assignments.keys())
    tool_context.state["grouping_assignments"] = assignments
    return {"saved": len(decisions), "missing_count": len(missing), "missing_ids": list(missing)}
