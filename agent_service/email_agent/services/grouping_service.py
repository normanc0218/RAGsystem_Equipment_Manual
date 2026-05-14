"""
Three-layer group clustering:
  Layer 1 — vector similarity (Firestore KNN, COSINE distance)
  Layer 2 — structural signals (thread, sender, name overlap, recency)
  Layer 3 — AI fallback (gpt-4o-mini, only for ambiguous 0.70-0.95 band)

Also exposes cluster_by_vector() for pure-vector experiment (no LLM).
"""
import logging
import math
import re
from datetime import datetime, timezone

from openai import OpenAI

from . import firestore_service
from .embedding_service import get_embedding

logger = logging.getLogger(__name__)

_SUBJECT_PREFIX_RE = re.compile(r'^(re|fwd?|fw):\s*', re.IGNORECASE)
_BRACKET_RE = re.compile(r'^\[[^\]]+\]\s*')


def _clean_subject(subject: str) -> str:
    """Strip Re:/Fwd: prefixes and bracket tags for use as a group name."""
    s = subject.strip()
    while True:
        prev = s
        s = _SUBJECT_PREFIX_RE.sub('', s).strip()
        s = _BRACKET_RE.sub('', s).strip()
        if s == prev:
            break
    return s[:60] if s else "Untitled"


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def cluster_by_vector(emails: list[dict]) -> dict[str, dict]:
    """Assign group names using only vector similarity — no LLM calls.

    Step 1: embed each email's subject + snippet.
    Step 2: KNN each against existing Firestore groups (threshold 0.75).
    Step 3: greedy clustering for unmatched emails (threshold 0.72).
    Step 4: name new clusters from the first email's cleaned subject.
    """
    if not emails:
        return {}

    embeddings: dict[str, list[float]] = {}
    for email in emails:
        text = f"{email['subject']} {email.get('snippet', '')}"
        embeddings[email["id"]] = get_embedding(text)

    assignments: dict[str, str] = {}
    unmatched: list[dict] = []

    for email in emails:
        emb = embeddings[email["id"]]
        try:
            candidate = firestore_service.find_nearest_group(emb, limit=1)
        except Exception:
            candidate = None

        if candidate and candidate["similarity"] >= 0.75:
            assignments[email["id"]] = candidate["name"]
            logger.debug("Vector match: %s → %s (%.3f)", email["id"], candidate["name"], candidate["similarity"])
        else:
            unmatched.append(email)

    # Greedy intra-batch clustering for emails with no Firestore match
    clusters: list[tuple[list[float], list[dict], str]] = []

    for email in unmatched:
        emb = embeddings[email["id"]]
        best_idx, best_sim = -1, 0.0

        for i, (centroid, _, _) in enumerate(clusters):
            sim = _cosine_similarity(emb, centroid)
            if sim >= 0.72 and sim > best_sim:
                best_sim, best_idx = sim, i

        if best_idx >= 0:
            centroid, cluster_emails, name = clusters[best_idx]
            cluster_emails.append(email)
            n = len(cluster_emails)
            new_centroid = [(centroid[j] * (n - 1) + emb[j]) / n for j in range(len(emb))]
            clusters[best_idx] = (new_centroid, cluster_emails, name)
        else:
            clusters.append((list(emb), [email], _clean_subject(email["subject"])))

    for _, cluster_emails, name in clusters:
        for email in cluster_emails:
            assignments[email["id"]] = name

    logger.info("cluster_by_vector: %d matched Firestore, %d new clusters from %d unmatched",
                len(assignments) - len(unmatched), len(clusters), len(unmatched))

    return {
        eid: {
            "group_name": gname,
            "should_archive": False,
            "archive_reason": "",
            "needs_attention": False,
            "attention_reason": "",
        }
        for eid, gname in assignments.items()
    }


def find_or_create_group(
    project_name: str,
    email_ids: list[str],
    description: str = "",
    thread_id: str = "",
    sender: str = "",
    embedding: list[float] | None = None,
) -> dict:
    # Thread short-circuit: same thread always merges regardless of topic similarity
    if thread_id:
        thread_match = firestore_service.find_group_by_thread_id(thread_id)
        if thread_match:
            return _merge_into_group(thread_match, email_ids, sender, thread_id)

    if embedding is None:
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
