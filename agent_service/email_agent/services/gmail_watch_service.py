"""
Gmail push notification service.

Registers a Gmail watch() so Google delivers inbox-change notifications to
POST /gmail/push via Pub/Sub. Stores the historyId cursor and watch expiration
in Firestore under _config/gmail_watch so state survives server restarts.

Pub/Sub payload shape (after base64 decode):
    {"emailAddress": "user@gmail.com", "historyId": "12345"}
"""
import json
import logging
import os
from datetime import datetime, timezone

from openai import OpenAI

logger = logging.getLogger(__name__)

_CONFIG = "_config"
_WATCH_DOC = "gmail_watch"


def _db():
    from .firestore_service import _db as get_db
    return get_db()


def _get_watch_state() -> dict:
    snap = _db().collection(_CONFIG).document(_WATCH_DOC).get()
    return snap.to_dict() if snap.exists else {}


def _save_watch_state(updates: dict) -> None:
    _db().collection(_CONFIG).document(_WATCH_DOC).set(updates, merge=True)


# ── Watch registration ────────────────────────────────────────────────────────

def start_watch() -> dict:
    """Register Gmail push notifications to the configured Pub/Sub topic."""
    from .email_provider import GmailProvider

    topic = os.getenv("GMAIL_PUBSUB_TOPIC")
    if not topic:
        raise RuntimeError("GMAIL_PUBSUB_TOPIC is not set in environment")

    service = GmailProvider()._get_service()
    result = service.users().watch(
        userId="me",
        body={
            "topicName": topic,
            "labelIds": ["INBOX"],
            "labelFilterBehavior": "INCLUDE",
        },
    ).execute()

    history_id = str(result["historyId"])
    expiration_dt = datetime.fromtimestamp(int(result["expiration"]) / 1000, tz=timezone.utc)

    _save_watch_state({
        "history_id": history_id,
        "expiration": expiration_dt.isoformat(),
        "registered_at": datetime.now(tz=timezone.utc).isoformat(),
    })

    logger.info("Gmail watch registered — historyId=%s expires=%s", history_id, expiration_dt.isoformat())
    return {"history_id": history_id, "expiration": expiration_dt.isoformat()}


def renew_if_needed() -> None:
    """Call on server startup — registers or renews the watch if expiring within 24 h."""
    state = _get_watch_state()

    if not state or not state.get("expiration"):
        logger.info("No Gmail watch found — registering.")
        start_watch()
        return

    expiration = datetime.fromisoformat(state["expiration"])
    if expiration.tzinfo is None:
        expiration = expiration.replace(tzinfo=timezone.utc)

    hours_left = (expiration - datetime.now(tz=timezone.utc)).total_seconds() / 3600

    if hours_left < 24:
        logger.info("Gmail watch expires in %.1f h — renewing.", hours_left)
        start_watch()
    else:
        logger.info("Gmail watch is active — %.1f h remaining.", hours_left)


# ── Notification processing ───────────────────────────────────────────────────

def process_push_notification(payload: dict) -> dict:
    """
    Handle a decoded Pub/Sub message from Gmail.

    Fetches the Gmail history since the last stored cursor, then:
    - messagesAdded  → classify with GPT → group in Firestore → apply Gmail label
    - labelsAdded    → user manually labelled an email → sync group to Firestore
    """
    from .email_provider import GmailProvider
    from .grouping_service import find_or_create_group
    from .firestore_service import mark_email_processed, get_processed_email_ids

    new_history_id = str(payload.get("historyId", ""))
    if not new_history_id:
        return {"processed": 0, "error": "no historyId in payload"}

    state = _get_watch_state()
    last_history_id = state.get("history_id")

    if not last_history_id:
        _save_watch_state({"history_id": new_history_id})
        return {"processed": 0, "message": "first notification — cursor saved"}

    provider = GmailProvider()
    service = provider._get_service()
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"

    # ── Fetch history since last cursor ───────────────────────────────────────
    try:
        history_resp = service.users().history().list(
            userId="me",
            startHistoryId=last_history_id,
            historyTypes=["messageAdded", "labelAdded"],
        ).execute()
    except Exception as exc:
        logger.warning("history.list failed: %s", exc)
        _save_watch_state({"history_id": new_history_id})
        return {"processed": 0, "error": str(exc)}

    records = history_resp.get("history", [])
    new_message_ids: set[str] = set()
    label_changes: list[dict] = []

    for record in records:
        for msg in record.get("messagesAdded", []):
            new_message_ids.add(msg["message"]["id"])
        for lc in record.get("labelsAdded", []):
            label_changes.append(lc)

    # Skip already-processed emails
    if new_message_ids:
        already = get_processed_email_ids(list(new_message_ids))
        new_message_ids -= already

    processed = 0
    client = OpenAI()

    # ── New messages: batch-fetch metadata → batch classify → group + label ───
    #
    # How the batch process works:
    #   Step A — collect all new message IDs from the history response (already done above)
    #   Step B — fetch metadata for ALL of them in one Gmail batch HTTP request
    #   Step C — classify ALL emails in one GPT call (same prompt format as /organize)
    #   Step D — for each classified email: find_or_create_group, mark processed, label
    #
    # This means 5 new emails cost 1 Gmail batch call + 1 GPT call instead of 10 calls.

    # Step B — batch metadata fetch
    new_emails: list[dict] = []
    if new_message_ids:
        new_emails = provider._batch_fetch_metadata([{"id": mid} for mid in new_message_ids])

    # Step C — classify all emails in one GPT call
    if new_emails:
        lines = [
            f"{i + 1}. Subject: {e['subject']} | Preview: {e['snippet']}"
            for i, e in enumerate(new_emails)
        ]
        prompt = (
            "Classify each email into a group name using the format: "
            "{Company} {Machine/Product} {Problem/Topic} (max 6 words).\n\n"
            + "\n".join(lines)
            + '\n\nReturn JSON: {"emails": [{"index": 1, "group_name": "...", '
            '"should_archive": false, "archive_reason": ""}]}'
        )
        cls_by_index: dict[int, dict] = {}
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": (
                        "Classify emails into groups using {Company} {Machine/Product} {Problem/Topic}. "
                        "Return only valid JSON."
                    )},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            data = json.loads(resp.choices[0].message.content)
            cls_by_index = {c.get("index"): c for c in data.get("emails", [])}
        except Exception as exc:
            logger.warning("Batch classify failed in push handler: %s", exc)

        # Step D — group, mark, label each email
        for i, email in enumerate(new_emails):
            cls = cls_by_index.get(i + 1, {"group_name": "Uncategorized", "should_archive": False})
            group_name = cls.get("group_name", "Uncategorized")
            try:
                result = find_or_create_group(
                    project_name=group_name,
                    email_ids=[email["id"]],
                    sender=email["from"],
                    thread_id=email["thread_id"],
                )
                mark_email_processed(
                    email_id=email["id"],
                    group_id=result["group_id"],
                    subject=email["subject"],
                    sender=email["from"],
                    date=email["date"],
                    snippet=email["snippet"],
                )
                label_name = group_name.replace("/", "-").replace("\\", "-").strip()[:100]
                if not dry_run:
                    try:
                        provider.label_email(email["id"], label_name)
                    except Exception as exc:
                        logger.warning("label_email failed for %s: %s", email["id"], exc)
                processed += 1
                logger.info("Auto-processed: '%s' → group '%s'", email["subject"], group_name)
            except Exception as exc:
                logger.warning("Failed to save %s: %s", email["id"], exc)

    # ── Manual label changes: sync label name → Firestore group ──────────────
    system_prefixes = ("INBOX", "SENT", "DRAFT", "SPAM", "TRASH",
                       "UNREAD", "STARRED", "IMPORTANT", "CATEGORY_")
    try:
        all_labels = service.users().labels().list(userId="me").execute().get("labels", [])
        label_map = {lbl["id"]: lbl["name"] for lbl in all_labels}
    except Exception:
        label_map = {}

    for lc in label_changes:
        msg_id = lc.get("message", {}).get("id")
        if not msg_id:
            continue
        for label_id in lc.get("labelIds", []):
            if any(label_id.startswith(p) for p in system_prefixes):
                continue
            label_name = label_map.get(label_id, "")
            if not label_name:
                continue
            try:
                detail = service.users().messages().get(
                    userId="me", id=msg_id, format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                ).execute()
                headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
                result = find_or_create_group(
                    project_name=label_name,
                    email_ids=[msg_id],
                    sender=headers.get("From", ""),
                    thread_id=detail.get("threadId", ""),
                )
                mark_email_processed(
                    email_id=msg_id,
                    group_id=result["group_id"],
                    subject=headers.get("Subject", "(no subject)"),
                    sender=headers.get("From", ""),
                    date=headers.get("Date", ""),
                    snippet=detail.get("snippet", ""),
                )
                processed += 1
                logger.info("Synced manual label '%s' for message %s → group %s",
                            label_name, msg_id, result["group_id"])
            except Exception as exc:
                logger.warning("Label sync failed for %s: %s", msg_id, exc)

    _save_watch_state({"history_id": new_history_id})

    return {
        "processed": processed,
        "new_messages": len(new_message_ids),
        "label_syncs": len(label_changes),
    }
