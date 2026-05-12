"""
Email tools — read and write actions against the Gmail inbox.

State keys read from tool_context:
  user_id  (str)  Slack user ID, used to attribute action log entries.
  dry_run  (bool) When True, skips all Gmail write calls.
"""
import json
import logging
import re

from google.adk.tools import ToolContext

logger = logging.getLogger(__name__)

BATCH_SIZE = 20


def _sender_domain(sender: str) -> str:
    """Extract domain from a sender string like 'Alice <alice@github.com>'."""
    match = re.search(r"@([\w.\-]+)", sender)
    return match.group(1).lower() if match else "unknown"


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
    from ..services.email_provider import get_email_provider
    from ..services.grouping_service import find_or_create_group

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

    if tool_context:
        from datetime import datetime
        tool_context.state["last_sync_time"] = datetime.utcnow().isoformat()

    return {"labels_synced": len(results), "groups": results}


def sync_gmail_labels_if_needed(tool_context: ToolContext = None) -> dict:
    """Sync Gmail labels into the semantic group DB only when needed."""
    from ..services.email_provider import get_email_provider
    from ..services.firestore_service import list_groups

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

    from ..database import SessionLocal
    from ..models.action_log import ActionLog
    from ..services.email_provider import get_email_provider
    from ..services.firestore_service import get_processed_email_ids, mark_email_processed, update_group
    from ..services.grouping_service import find_or_create_group

    user_id = tool_context.state.get("user_id", "unknown") if tool_context else "unknown"
    dry_run = tool_context.state.get("dry_run", True) if tool_context else True

    provider = get_email_provider()
    emails = provider.fetch_emails(max_results=min(max_results, 500), random_sample=random_sample)

    total_fetched = len(emails)
    processed_ids = get_processed_email_ids([e["id"] for e in emails])
    emails = [e for e in emails if e["id"] not in processed_ids]
    already_processed = total_fetched - len(emails)

    if not emails:
        return {"processed": 0, "grouped": 0, "archived": 0, "already_processed": already_processed,
                "message": f"All {already_processed} fetched emails are already organised."}

    logger.info("batch_process_emails: %d new, %d already processed (skipped)", len(emails), already_processed)

    # ── Step 1: load existing groups from Firestore ───────────────────────────
    from ..services.firestore_service import list_groups
    existing_groups = {g["name"]: g.get("summary", "") for g in list_groups()}

    # ── Step 2: pre-group by sender domain, classify semantically within each ─
    client = OpenAI()

    domain_buckets: dict[str, list[dict]] = {}
    for email in emails:
        domain = _sender_domain(email.get("from", ""))
        domain_buckets.setdefault(domain, []).append(email)

    email_cls: dict[str, dict] = {}  # email_id → classification

    for domain, bucket in domain_buckets.items():
        # Seed only with groups whose name was produced while processing this domain
        # (existing_groups from Firestore is kept separate to avoid cross-domain noise)
        domain_existing = {k: v for k, v in existing_groups.items()}

        for i in range(0, len(bucket), BATCH_SIZE):
            batch = bucket[i: i + BATCH_SIZE]
            lines = [
                f"{j + 1}. Subject: {e['subject']} | Preview: {e['snippet']}"
                for j, e in enumerate(batch)
            ]
            existing_str = ""
            if domain_existing:
                existing_str = "\n\nExisting groups (reuse exact name if the email fits):\n" + \
                    "\n".join(f"- {name}: {desc}" for name, desc in domain_existing.items())

            prompt = (
                f"These emails are all from the domain @{domain}.\n"
                "Classify each one into a sub-group based ONLY on the semantic meaning "
                "of the subject and body — do NOT use the sender domain as the group name.\n\n"
                + "\n".join(lines)
                + existing_str
                + "\n\nReturn JSON:\n"
                '{"emails": [{"index": 1, "group_name": "Company Machine Problem", '
                '"should_archive": false, "archive_reason": "", '
                '"needs_attention": false, "attention_reason": ""}]}\n'
                "Rules:\n"
                "- group_name MUST follow the format: {Company} {Machine/Product} {Problem/Topic}\n"
                "  Examples: 'Siemens S7-1500 Overheating', 'ABB Robot Arm Calibration Fault',\n"
                "            'Fanuc CNC Spindle Error', 'Bosch Pump Pressure Drop'\n"
                "- Include as many of the three parts as the email makes clear\n"
                "- If no machine/product is mentioned, use: {Company} {Topic}, e.g. 'Siemens Billing'\n"
                "- If an existing group fits exactly, use its exact name\n"
                "- Max 6 words\n"
                "- should_archive=true ONLY if clearly done and needs no action "
                "(paid invoice, resolved ticket, read-only notification)\n"
                "- should_archive=false if the email may need a reply or follow-up\n"
                "- Promotions/newsletters: group as 'Promotions' or 'Newsletters', "
                "archive only if clearly one-way\n"
                "- needs_attention=true if the email requires urgent action or a reply "
                "(fault alarm, overdue payment, deadline, escalation, safety issue)\n"
                "- attention_reason: one short sentence explaining why it needs attention"
            )
            try:
                resp = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": (
                            "Classify emails into project groups using the format: "
                            "{Company} {Machine/Product} {Problem/Topic}. "
                            "Extract the company name, equipment or product model, and the issue or topic "
                            "from the subject and body. Reuse existing group names when the topic matches. "
                            "Return only valid JSON."
                        )},
                        {"role": "user", "content": prompt},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0,
                )
                data = json.loads(resp.choices[0].message.content)
                cls_by_index = {c.get("index"): c for c in data.get("emails", [])}
                for j, email in enumerate(batch):
                    cls = cls_by_index.get(j + 1, {
                        "group_name": "Uncategorized", "should_archive": False, "archive_reason": ""
                    })
                    email_cls[email["id"]] = cls
                    name = cls.get("group_name", "")
                    if name and name not in domain_existing:
                        domain_existing[name] = ""
                        existing_groups[name] = ""
            except Exception as exc:
                logger.warning("Classification batch domain=%s batch=%d failed: %s", domain, i // BATCH_SIZE, exc)
                for email in batch:
                    email_cls[email["id"]] = {
                        "group_name": "Uncategorized", "should_archive": False, "archive_reason": ""
                    }

    # ── Step 3: aggregate ALL emails by group_name ───────────────────────────
    groups_to_save: dict[str, dict] = {}
    needs_attention: list[dict] = []

    for email in emails:
        cls = email_cls.get(email["id"], {"group_name": "Uncategorized", "should_archive": False, "archive_reason": ""})
        name = cls.get("group_name", "Uncategorized")
        if name not in groups_to_save:
            groups_to_save[name] = {
                "emails": [],
                "senders": set(),
                "thread_ids": set(),
            }
        groups_to_save[name]["emails"].append((email, cls))
        if cls.get("needs_attention"):
            needs_attention.append({
                "subject": email["subject"],
                "from": email.get("from", ""),
                "group": name,
                "reason": cls.get("attention_reason", ""),
            })

    # ── Step 4: save groups + archive done emails ─────────────────────────────
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

            # Sanitise label name: Gmail labels cannot contain these characters
            label_name = name.replace("/", "-").replace("\\", "-").strip()[:100]

            for email, cls in data["emails"]:
                mark_email_processed(
                    email_id=email["id"],
                    group_id=result["group_id"],
                    subject=email.get("subject", ""),
                    sender=email.get("from", ""),
                    date=email.get("date", ""),
                    snippet=email.get("snippet", ""),
                )
                # Apply Gmail label so the email is visible in the right group
                label_status = "dry_run" if dry_run else "success"
                if not dry_run:
                    try:
                        provider.label_email(email["id"], label_name)
                    except Exception as exc:
                        logger.warning("label_email failed for %s: %s", email["id"], exc)
                        label_status = "error"
                log = ActionLog(
                    user=user_id,
                    action="label",
                    email_id=email["id"],
                    email_subject=email["subject"],
                    label=label_name,
                    status=label_status,
                )
                db.add(log)
                db.commit()

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

    result = {
        "fetched": total_fetched,
        "already_processed": already_processed,
        "processed": len(emails),
        "grouped": sum(g["emails"] for g in saved_groups.values()),
        "archived": len(archived),
        "groups": saved_groups,
        "archived_emails": [{"subject": a["subject"], "log_id": a["log_id"]} for a in archived],
        "needs_attention": needs_attention,
    }

    if tool_context and len(emails) > 0:
        from datetime import datetime
        tool_context.state["interaction_history"] = tool_context.state.get("interaction_history", []) + [{
            "action": "organize",
            "fetched": total_fetched,
            "processed": len(emails),
            "groups": len(saved_groups),
            "archived": len(archived),
            "timestamp": datetime.utcnow().isoformat(),
        }]
        tool_context.state["emails_processed_total"] = (
            tool_context.state.get("emails_processed_total", 0) + len(emails)
        )

    return result


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
    from ..database import SessionLocal
    from ..models.action_log import ActionLog
    from ..services.email_provider import get_email_provider

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
