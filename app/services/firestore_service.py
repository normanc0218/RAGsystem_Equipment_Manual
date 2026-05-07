"""
Firestore data layer — manages two collections:
  email_summaries  — one doc per email, keyed by email_id
  email_groups     — one doc per project group, keyed by group_id
"""
import os
import uuid
from datetime import datetime, timezone

from google.cloud import firestore
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

SUMMARIES = "email_summaries"
GROUPS = "email_groups"

_client: firestore.Client | None = None


def _db() -> firestore.Client:
    global _client
    if _client is None:
        project = os.getenv("GOOGLE_CLOUD_PROJECT")
        database = os.getenv("FIRESTORE_DATABASE", "(default)")
        creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if creds_path:
            from google.oauth2 import service_account
            credentials = service_account.Credentials.from_service_account_file(
                creds_path,
                scopes=["https://www.googleapis.com/auth/datastore"],
            )
            _client = firestore.Client(project=project, credentials=credentials, database=database)
        else:
            _client = firestore.Client(project=project, database=database)
    return _client


# ── email_summaries ───────────────────────────────────────────────────────────

def save_email_summary(doc: dict) -> str:
    email_id = doc["email_id"]
    _db().collection(SUMMARIES).document(email_id).set(doc)
    return email_id


def get_email_summary(email_id: str) -> dict | None:
    snap = _db().collection(SUMMARIES).document(email_id).get()
    return snap.to_dict() if snap.exists else None


def mark_email_processed(email_id: str, group_id: str) -> None:
    _db().collection(SUMMARIES).document(email_id).update({
        "group_id": group_id,
        "processed": True,
    })


# ── email_groups ──────────────────────────────────────────────────────────────

def save_group(doc: dict) -> str:
    group_id = doc.get("group_id") or uuid.uuid4().hex[:8]
    doc = {**doc, "group_id": group_id}
    if "embedding" in doc and not isinstance(doc["embedding"], Vector):
        doc["embedding"] = Vector(doc["embedding"])
    if "created_at" not in doc:
        doc["created_at"] = datetime.now(tz=timezone.utc)
    if "last_activity" not in doc:
        doc["last_activity"] = doc["created_at"]
    _db().collection(GROUPS).document(group_id).set(doc)
    return group_id


def get_group(group_id: str) -> dict | None:
    snap = _db().collection(GROUPS).document(group_id).get()
    if not snap.exists:
        return None
    return _strip_vector(snap.to_dict())


def update_group(group_id: str, updates: dict) -> None:
    if "embedding" in updates and not isinstance(updates["embedding"], Vector):
        updates["embedding"] = Vector(updates["embedding"])
    updates["last_activity"] = datetime.now(tz=timezone.utc)
    _db().collection(GROUPS).document(group_id).update(updates)


def list_groups() -> list[dict]:
    docs = _db().collection(GROUPS).stream()
    return [_strip_vector(d.to_dict()) for d in docs]


def find_nearest_group(embedding: list[float], limit: int = 1) -> dict | None:
    results = (
        _db()
        .collection(GROUPS)
        .find_nearest(
            vector_field="embedding",
            query_vector=Vector(embedding),
            distance_measure=DistanceMeasure.COSINE,
            limit=limit,
            distance_result_field="cosine_distance",
        )
        .get()
    )
    docs = list(results)
    if not docs:
        return None
    data = docs[0].to_dict()
    distance = data.pop("cosine_distance", 1.0)
    data["similarity"] = round(1.0 - distance, 4)
    return _strip_vector(data)


def _strip_vector(doc: dict) -> dict:
    doc.pop("embedding", None)
    return doc
