"""
Email tools — read and write actions against the Gmail inbox.

State keys read from tool_context:
  user_id  (str)  Slack user ID, used to attribute action log entries.
  dry_run  (bool) When True, skips all Gmail write calls.
"""
import json
import logging

from google.adk.tools import ToolContext

logger = logging.getLogger(__name__)

BATCH_SIZE = 20


def sync_gmail_labels(tool_context: ToolContext = None) -> dict:
    """Phase 1: Bootstrap Firestore from the user's existing Gmail labels.

    For each user-created label: creates a Firestore group, fetches its emails,
    generates an embedding, and marks those emails as processed. Run this once
    before batch_process_emails so the vector DB reflects the user's existing
    organization style.

    Args:
        tool_context: Injected by ADK — do not pass manually.

    Returns:
        Dict with labels_synced count and per-label group summary.
    """
    from app.services.email_provider import get_email_provider
    from app.services.grouping_service import find_or_create_group

    provider = get_email_provider()
    labels = provider.list_user_labels()

    results = {}
    for label in labels:
        emails = provider.fetch_emails_by_label(label["id"], max_results=100)
        if not emails:
            continue
        email_ids = [e["id"] for e in emails]
        senders = list({e["from"] for e in emails})
        thread_ids = list({e["thread_id"] for e in emails if e.get("thread_id")})
        result = find_or_create_group(
            project_name=label["name"],
            email_ids=email_ids,
            description=f"Emails labelled '{label['name']}' in Gmail",
            sender=senders[0] if senders else "",
            thread_id=thread_ids[0] if thread_ids else "",
        )
        results[label["name"]] = result

    return {"labels_synced": len(results), "groups": results}


def sync_gmail_labels_if_needed(tool_context: ToolContext = None) -> dict:
    """Sync Gmail labels into the semantic group DB only when needed."""
    from app.services.email_provider import get_email_provider
    from app.services.firestore_service import list_groups

    provider = get_email_provider()
    existing_groups = {g["name"].lower() for g in list_groups()}
    labels = provider.list_user_labels()
    new_labels = [label for label in labels if label["name"].lower() not in existing_groups]

    if not existing_groups or new_labels:
        return sync_gmail_labels(tool_context)

    return {"labels_synced": 0, "message": "Existing label groups already seeded; no sync needed."}


def inbox_processing_agent(
    max_results: int = 200,
    random_sample: bool = True,
    tool_context: ToolContext = None,
) -> dict:
    """Alias for the inbox processing sub-agent."""
    return batch_process_emails(max_results=max_results, random_sample=random_sample, tool_context=tool_context)


def batch_process_emails(
    max_results: int = 200,
    random_sample: bool = True,
    tool_context: ToolContext = None,
) -> dict:
    """Phase 2: Fetch and process ALL unprocessed emails in one tool call.

    Python loops through every email — the LLM never has to iterate.
    Emails are classified in batches of 20, then aggregated by group_name
    before saving to Firestore (so same-named groups are never split).
    Summaries are generated from already-fetched data — no re-fetch needed.

    Args:
        max_results: Max emails to fetch (default 200, max 500).
        random_sample: When True, fetch a larger pool and sample randomly.
        tool_context: Injected by ADK — do not pass manually.

    Returns:
        Dict with total processed, groups, archived emails, and summaries.
    """
    from openai import OpenAI

    from app.database import SessionLocal
    from app.models.action_log import ActionLog
    from app.services.email_provider import get_email_provider
    from app.services.firestore_service import get_processed_email_ids, mark_email_processed, update_group
    from app.services.grouping_service import find_or_create_group

    user_id = tool_context.state.get("user_id", "unknown") if tool_context else "unknown"
    dry_run = tool_context.state.get("dry_run", True) if tool_context else True

    provider = get_email_provider()
    emails = provider.fetch_emails(max_results=min(max_results, 500), random_sample=random_sample)

    processed_ids = get_processed_email_ids([e["id"] for e in emails])
    emails = [e for e in emails if e["id"] not in processed_ids]

    if not emails:
        return {"processed": 0, "grouped": 0, "archived": 0,
                "message": "All emails are already organized."}

    logger.info("batch_process_emails: %d unprocessed emails to classify", len(emails))

    # ── Step 1: load existing groups from Firestore ───────────────────────────
    from app.services.firestore_service import list_groups
    existing_groups = {g["name"]: g.get("summary", "") for g in list_groups()}

    # ── Step 2: classify all emails in batches ────────────────────────────────
    client = OpenAI()
    classifications: list[dict] = []

    for i in range(0, len(emails), BATCH_SIZE):
        batch = emails[i: i + BATCH_SIZE]
        lines = [
            f"{j + 1}. Subject: {e['subject']} | From: {e['from']} | Preview: {e['snippet']}"
            for j, e in enumerate(batch)
        ]
        existing_str = ""
        if existing_groups:
            existing_str = "\n\nExisting groups (reuse these exact names if the email fits):\n" + \
                "\n".join(f"- {name}: {desc}" for name, desc in existing_groups.items())

        prompt = (
            "\n".join(lines)
            + existing_str
            + "\n\nClassify each email. Return JSON:\n"
            '{"emails": [{"index": 1, "group_name": "Short Topic Name", '
            '"should_archive": false, "archive_reason": ""}]}\n'
            "Rules:\n"
            "- Every email MUST have a group_name (5 words or fewer)\n"
            "- If an existing group fits, use its exact name\n"
            "- should_archive=true ONLY if the thread/project is clearly done and needs no further action "
            "(e.g. paid invoice, resolved support ticket, completed transaction, read-only notification)\n"
            "- should_archive=false if the email may need a reply or follow-up\n"
            "- Promotions and newsletters get a group name like 'Promotions' or 'Newsletters' — "
            "archive them only if they are clearly one-way and need no action"
        )
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Classify emails into project groups. Reuse existing group names when the topic matches. Return only valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            data = json.loads(resp.choices[0].message.content)
            batch_cls = sorted(data.get("emails", []), key=lambda x: x.get("index", 0))
            classifications.extend(batch_cls)
            # Add newly created group names so later batches can reuse them too
            for cls in batch_cls:
                name = cls.get("group_name", "")
                if name and name not in existing_groups:
                    existing_groups[name] = ""
        except Exception as exc:
            logger.warning("Classification batch %d failed: %s", i // BATCH_SIZE, exc)
            classifications.extend([
                {"index": j + 1, "group_name": "Uncategorized", "description": "",
                 "should_archive": False, "archive_reason": ""}
                for j in range(len(batch))
            ])

    # ── Step 2: aggregate ALL emails by group_name ───────────────────────────
    # Every email gets grouped. should_archive is a secondary action, not a routing decision.
    groups_to_save: dict[str, dict] = {}

    for email, cls in zip(emails, classifications):
        name = cls.get("group_name", "Uncategorized")
        if name not in groups_to_save:
            groups_to_save[name] = {
                "emails": [],
                "senders": set(),
                "thread_ids": set(),
            }
        groups_to_save[name]["emails"].append((email, cls))

    # ── Step 3: save groups + archive done emails ─────────────────────────────
    saved_groups: dict[str, dict] = {}
    archived: list[dict] = []
    db = SessionLocal()

    try:
        for name, data in groups_to_save.items():
            email_objs = [e for e, _ in data["emails"]]
            email_ids = [e["id"] for e in email_objs]
            senders = list({e.get("from", "") for e in email_objs})
            thread_ids = list({e.get("thread_id", "") for e in email_objs if e.get("thread_id")})

            result = find_or_create_group(
                project_name=name,
                email_ids=email_ids,
                description="",
                sender=senders[0] if senders else "",
                thread_id=thread_ids[0] if thread_ids else "",
            )

            for email, cls in data["emails"]:
                mark_email_processed(
                    email_id=email["id"],
                    group_id=result["group_id"],
                    subject=email.get("subject", ""),
                    sender=email.get("from", ""),
                    date=email.get("date", ""),
                    snippet=email.get("snippet", ""),
                )
                # Archive emails whose project/thread is done
                if cls.get("should_archive"):
                    status = "dry_run" if dry_run else "success"
                    if not dry_run:
                        provider.archive_email(email["id"])
                    log = ActionLog(
                        user=user_id,
                        action="archive",
                        email_id=email["id"],
                        email_subject=email["subject"],
                        status=status,
                    )
                    db.add(log)
                    db.commit()
                    db.refresh(log)
                    archived.append({
                        "subject": email["subject"],
                        "group": name,
                        "reason": cls.get("archive_reason", ""),
                        "log_id": log.id,
                    })

            # Generate summary from full email bodies
            body_lines = []
            for email_obj, _ in data["emails"]:
                try:
                    body = provider.get_email_body(email_obj["id"])
                    body_lines.append(
                        f"Subject: {email_obj['subject']}\nFrom: {email_obj.get('from', '')}\n{body[:1000]}"
                    )
                except Exception:
                    body_lines.append(
                        f"Subject: {email_obj['subject']}\nFrom: {email_obj.get('from', '')}\n{email_obj.get('snippet', '')}"
                    )
            try:
                summary_resp = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": "Summarize these emails in 2-3 sentences. Be concise and actionable."},
                        {"role": "user", "content": f"Group: {name}\n\n" + "\n\n---\n\n".join(body_lines)},
                    ],
                    max_tokens=150,
                )
                summary = summary_resp.choices[0].message.content.strip()
                update_group(result["group_id"], {"summary": summary})
            except Exception as exc:
                logger.warning("Summary failed for group %s: %s", name, exc)
                summary = ""

            saved_groups[name] = {
                "group_id": result["group_id"],
                "emails": len(email_ids),
                "archived": sum(1 for _, cls in data["emails"] if cls.get("should_archive")),
                "action": result["action"],
                "summary": summary,
            }
    finally:
        db.close()

    return {
        "processed": len(emails),
        "grouped": sum(g["emails"] for g in saved_groups.values()),
        "archived": len(archived),
        "groups": saved_groups,
        "archived_emails": [{"subject": a["subject"], "log_id": a["log_id"]} for a in archived],
    }


def archive_email(
    email_id: str,
    email_subject: str,
    reason: str,
    tool_context: ToolContext = None,
) -> dict:
    """Archive a single email. Used for manual one-off archiving by the agent.

    Args:
        email_id: The unique Gmail message ID.
        email_subject: Subject line of the email (for the log).
        reason: Why this email is being archived.
        tool_context: Injected by ADK — do not pass manually.

    Returns:
        Dict with log_id, status, and dry_run flag.
    """
    from app.database import SessionLocal
    from app.models.action_log import ActionLog
    from app.services.email_provider import get_email_provider

    user_id = tool_context.state.get("user_id", "unknown") if tool_context else "unknown"
    dry_run = tool_context.state.get("dry_run", True) if tool_context else True

    provider = get_email_provider()
    status = "dry_run" if dry_run else "success"

    if not dry_run:
        provider.archive_email(email_id)

    db = SessionLocal()
    try:
        log = ActionLog(
            user=user_id,
            action="archive",
            email_id=email_id,
            email_subject=email_subject,
            status=status,
        )
        db.add(log)
        db.commit()
        db.refresh(log)
        return {"log_id": log.id, "status": status, "dry_run": dry_run, "reason": reason}
    finally:
        db.close()
