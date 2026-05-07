"""
Three-layer group clustering:
  Layer 1 — vector similarity (Firestore KNN, COSINE distance)
  Layer 2 — structural signals (thread, sender, name overlap, recency)
  Layer 3 — AI fallback (gpt-4o-mini, only for ambiguous 0.70-0.95 band)
"""
import logging
from datetime import datetime, timezone

from openai import OpenAI

from app.services import firestore_service
from app.services.embedding_service import get_embedding

logger = logging.getLogger(__name__)


def find_or_create_group(
    project_name: str,
    email_ids: list[str],
    description: str = "",
    thread_id: str = "",
    sender: str = "",
) -> dict:
    embedding = get_embedding(f"{project_name} {description}")

    candidate = None
    try:
        candidate = firestore_service.find_nearest_group(embedding, limit=1)
    except Exception as exc:
        logger.warning("Vector search unavailable (index not ready?): %s — creating new group", exc)

    if candidate is None or candidate["similarity"] < 0.70:
        return _create_group(project_name, email_ids, description, embedding, sender, thread_id)

    if candidate["similarity"] > 0.95:
        return _merge_into_group(candidate, email_ids, sender, thread_id)

    score = 0
    if thread_id and thread_id in (candidate.get("thread_ids") or []):
        score += 1
    if sender and sender in (candidate.get("senders") or []):
        score += 1
    if project_name.lower() in candidate.get("name", "").lower():
        score += 1
    if _days_since(candidate.get("last_activity")) < 30:
        score += 1

    if score >= 2:
        return _merge_into_group(candidate, email_ids, sender, thread_id)

    decision = _ai_decide_group(project_name, description, candidate)
    if decision == "join":
        return _merge_into_group(candidate, email_ids, sender, thread_id)
    return _create_group(project_name, email_ids, description, embedding, sender, thread_id)


def _create_group(project_name, email_ids, description, embedding, sender, thread_id):
    group_id = firestore_service.save_group({
        "name": project_name,
        "description": description,
        "summary": "",
        "embedding": embedding,
        "email_ids": list(email_ids),
        "senders": [sender] if sender else [],
        "thread_ids": [thread_id] if thread_id else [],
        "email_count": len(email_ids),
    })
    return {"group_id": group_id, "name": project_name, "email_count": len(email_ids), "action": "created"}


def _merge_into_group(candidate, email_ids, sender, thread_id):
    group_id = candidate["group_id"]
    merged_ids = list(set(candidate.get("email_ids") or []) | set(email_ids))
    senders = set(candidate.get("senders") or [])
    threads = set(candidate.get("thread_ids") or [])
    if sender:
        senders.add(sender)
    if thread_id:
        threads.add(thread_id)
    firestore_service.update_group(group_id, {
        "email_ids": merged_ids,
        "senders": list(senders),
        "thread_ids": list(threads),
        "email_count": len(merged_ids),
    })
    return {"group_id": group_id, "name": candidate["name"], "email_count": len(merged_ids), "action": "merged"}


def _ai_decide_group(project_name, description, candidate):
    prompt = (
        f"Existing group: '{candidate['name']}' — {candidate.get('description', '')}\n"
        f"New email topic: '{project_name}' — {description}\n\n"
        "Should the new email join the existing group, or does it belong to a separate group?\n"
        "Reply with exactly one word: join or new."
    )
    response = OpenAI().chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Decide if emails belong to the same project group. Reply only 'join' or 'new'."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=5,
        temperature=0,
    )
    answer = response.choices[0].message.content.strip().lower()
    return "join" if "join" in answer else "new"


def _days_since(timestamp) -> float:
    if timestamp is None:
        return 999.0
    if hasattr(timestamp, "tzinfo") and timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return (datetime.now(tz=timezone.utc) - timestamp).total_seconds() / 86400
