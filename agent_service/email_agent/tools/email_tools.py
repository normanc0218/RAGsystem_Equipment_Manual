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


def pre_process_emails(
    max_results: int = 20,
    tool_context: ToolContext = None,
) -> dict:
    """Stages 1-3 of the email processing pipeline: fetch, filter, and pre-cluster emails.

    Stage 1 — User-labelled: emails already labelled by the user keep their label as the group
              name and are ingested into the vector DB.
    Stage 2 — Template detection: automated/template emails (low entropy, keyword signals) are
              routed to "Misc" and marked for archiving.
    Stage 3 — Thread grouping: emails sharing a thread_id within this batch are grouped together.

    Emails that pass all three stages are stored in session state (emails_to_cluster) for
    the email_grouping_agent to handle in Stage 4.

    If GROUPING_MODE=vector, Stage 4 is also run here using pure embedding similarity
    (no LLM), and the result is pre-populated into grouping_assignments so the grouping
    agent can be skipped.

    Args:
        max_results: Maximum emails to fetch and process (capped at 20).
        tool_context: Injected by ADK — do not pass manually.

    Returns:
        Dict with scanned, already_processed, stage1/2/3 counts, remaining (Stage 4 input).
    """
    import os

    from ..services.email_provider import get_email_provider
    from ..services.embedding_service import get_embedding
    from ..services.firestore_service import get_processed_email_ids
    from ..services.grouping_service import cluster_by_vector, _clean_subject
    from ..services.metrics_service import metrics
    from ..services.template_service import is_template_email

    metrics.reset()
    provider = get_email_provider()
    target = min(max_results, 20)
    emails: list[dict] = []
    page_token = None
    total_scanned = 0

    while len(emails) < target:
        page, page_token = provider.fetch_emails_page(page_size=50, page_token=page_token)
        if not page:
            break
        total_scanned += len(page)
        page_processed = get_processed_email_ids([e["id"] for e in page])
        emails.extend(e for e in page if e["id"] not in page_processed)
        if not page_token:
            break

    emails = emails[:target]
    already_processed = total_scanned - len(emails)

    if not emails:
        if tool_context:
            tool_context.state["_all_emails"] = []
            tool_context.state["email_pre_cls"] = {}
            tool_context.state["emails_to_cluster"] = []
            tool_context.state["_total_scanned"] = total_scanned
            tool_context.state["_already_processed"] = already_processed
        return {
            "scanned": total_scanned,
            "already_processed": already_processed,
            "remaining": 0,
            "message": f"Scanned {total_scanned} emails — all already organised.",
        }

    logger.info("pre_process_emails: %d new out of %d scanned", len(emails), total_scanned)

    grouping_mode = os.getenv("GROUPING_MODE", "adk")  # "adk" | "vector" | "llm"
    user_label_map = {lbl["id"]: lbl["name"] for lbl in provider.list_user_labels()}
    email_pre_cls: dict[str, dict] = {}

    # ── Stage 1: user-labelled emails ────────────────────────────────────────
    remaining = []
    for email in emails:
        names = [user_label_map[lid] for lid in email.get("label_ids", []) if lid in user_label_map]
        if names:
            emb = get_embedding(f"{email['subject']} {email.get('snippet', '')}")
            email_pre_cls[email["id"]] = {
                "group_name": names[0],
                "should_archive": False,
                "archive_reason": "already labelled by user",
                "needs_attention": False,
                "attention_reason": "",
                "_embedding": emb,
            }
        else:
            remaining.append(email)

    stage1_count = len(email_pre_cls)
    logger.info("Stage 1 (user-labelled): %d  remaining: %d", stage1_count, len(remaining))

    # ── Stage 2: template / automated email detection ────────────────────────
    primary = []
    for email in remaining:
        if is_template_email(email.get("snippet", ""), email.get("subject", "")):
            email_pre_cls[email["id"]] = {
                "group_name": "Misc",
                "should_archive": True,
                "archive_reason": "template/automated email",
                "needs_attention": False,
                "attention_reason": "",
            }
        else:
            primary.append(email)

    stage2_count = len(remaining) - len(primary)
    logger.info("Stage 2 (template→Misc): %d  remaining: %d", stage2_count, len(primary))

    # ── Stage 3: thread grouping ──────────────────────────────────────────────
    thread_map: dict[str, list[dict]] = {}
    to_cluster: list[dict] = []
    for email in primary:
        tid = email.get("thread_id", "")
        if tid:
            thread_map.setdefault(tid, []).append(email)
        else:
            to_cluster.append(email)

    for tid, thread_emails in thread_map.items():
        if len(thread_emails) >= 2:
            name = _clean_subject(thread_emails[0]["subject"])
            for email in thread_emails:
                email_pre_cls[email["id"]] = {
                    "group_name": name,
                    "should_archive": False,
                    "archive_reason": "",
                    "needs_attention": False,
                    "attention_reason": "",
                }
        else:
            to_cluster.extend(thread_emails)

    stage3_count = len(primary) - len(to_cluster)
    logger.info("Stage 3 (thread groups): %d  Stage 4 input: %d", stage3_count, len(to_cluster))

    # ── Stage 4 fast-path: vector mode (no LLM, no ADK agent) ────────────────
    # When GROUPING_MODE=vector, run pure embedding clustering here and pre-populate
    # grouping_assignments so finalize_email_processing can proceed directly.
    grouping_assignments: dict[str, dict] = {}
    if to_cluster and grouping_mode == "vector":
        vector_cls = cluster_by_vector(to_cluster)
        grouping_assignments.update(vector_cls)
        logger.info("Stage 4 (vector fast-path): %d emails clustered", len(vector_cls))

    if tool_context:
        tool_context.state["_all_emails"] = emails
        tool_context.state["email_pre_cls"] = email_pre_cls
        # In vector mode assignments are already computed — signal grouping agent to skip
        tool_context.state["emails_to_cluster"] = [] if grouping_assignments else to_cluster
        tool_context.state["_total_scanned"] = total_scanned
        tool_context.state["_already_processed"] = already_processed
        if grouping_assignments:
            tool_context.state["grouping_assignments"] = grouping_assignments

    return {
        "scanned": total_scanned,
        "already_processed": already_processed,
        "stage1_user_labelled": stage1_count,
        "stage2_templates": stage2_count,
        "stage3_thread_grouped": stage3_count,
        "remaining": len(to_cluster) if grouping_mode != "vector" else 0,
        "grouping_mode": grouping_mode,
    }


def finalize_email_processing(tool_context: ToolContext = None) -> dict:
    """Steps 5-6: save groups to Firestore, apply Gmail labels, archive, and summarize.

    Reads from session state (written by pre_process_emails + email_grouping_agent):
      _all_emails, email_pre_cls, grouping_assignments, _total_scanned, _already_processed.

    Merges Stage 1-3 pre-classifications with Stage 4 grouping assignments, then:
    - Creates or merges groups in Firestore.
    - Marks each email as processed.
    - Applies Gmail labels (skipped in dry_run mode).
    - Archives completed emails (skipped in dry_run mode).
    - Generates one-call incremental group summaries via GPT.

    Args:
        tool_context: Injected by ADK — do not pass manually.

    Returns:
        Dict with processed, grouped, archived, groups, needs_attention, api_metrics.
    """
    from openai import OpenAI

    from ..database import SessionLocal
    from ..models.action_log import ActionLog
    from ..services.email_provider import get_email_provider
    from ..services.firestore_service import get_processed_email_ids, mark_email_processed, update_group, list_groups
    from ..services.grouping_service import find_or_create_group
    from ..services.metrics_service import metrics

    user_id = tool_context.state.get("user_id", "unknown") if tool_context else "unknown"
    dry_run = tool_context.state.get("dry_run", True) if tool_context else True

    emails: list[dict] = tool_context.state.get("_all_emails", []) if tool_context else []
    email_pre_cls: dict = tool_context.state.get("email_pre_cls", {}) if tool_context else {}
    grouping_assignments: dict = tool_context.state.get("grouping_assignments", {}) if tool_context else {}
    total_scanned: int = tool_context.state.get("_total_scanned", 0) if tool_context else 0
    already_processed: int = tool_context.state.get("_already_processed", 0) if tool_context else 0

    if not emails:
        return {"processed": 0, "grouped": 0, "archived": 0, "already_processed": already_processed,
                "message": "No emails to finalize — run pre_process_emails first."}

    # Merge pre-classifications with Stage 4 grouping assignments
    email_cls: dict[str, dict] = {**email_pre_cls}
    for eid, cls in grouping_assignments.items():
        if eid not in email_cls:
            email_cls[eid] = cls

    # Fallback: any email not yet classified
    for email in emails:
        if email["id"] not in email_cls:
            email_cls[email["id"]] = {"group_name": "Uncategorized", "should_archive": False, "archive_reason": ""}

    existing_groups = {g["name"]: g.get("summary", "") for g in list_groups()}
    provider = get_email_provider()

    # ── Aggregate by group_name ───────────────────────────────────────────────
    groups_to_save: dict[str, dict] = {}
    needs_attention: list[dict] = []

    for email in emails:
        cls = email_cls[email["id"]]
        name = cls.get("group_name", "Uncategorized")
        if name not in groups_to_save:
            groups_to_save[name] = {"emails": []}
        groups_to_save[name]["emails"].append((email, cls))
        if cls.get("needs_attention"):
            needs_attention.append({
                "subject": email["subject"],
                "from": email.get("from", ""),
                "group": name,
                "reason": cls.get("attention_reason", ""),
            })

    # ── Step 5: save groups + archive ────────────────────────────────────────
    saved_groups: dict[str, dict] = {}
    archived: list[dict] = []
    db = SessionLocal()

    try:
        for name, data in groups_to_save.items():
            email_objs = [e for e, _ in data["emails"]]
            email_ids = [e["id"] for e in email_objs]
            senders = list({e.get("from", "") for e in email_objs})
            thread_ids = list({e.get("thread_id", "") for e in email_objs if e.get("thread_id")})
            pre_emb = next(
                (email_cls[e["id"]].get("_embedding") for e, _ in data["emails"]
                 if email_cls.get(e["id"], {}).get("_embedding")),
                None,
            )
            result = find_or_create_group(
                project_name=name,
                email_ids=email_ids,
                description="",
                sender=senders[0] if senders else "",
                thread_id=thread_ids[0] if thread_ids else "",
                embedding=pre_emb,
            )
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
                label_status = "dry_run" if dry_run else "success"
                if not dry_run:
                    try:
                        provider.label_email(email["id"], label_name)
                    except Exception as exc:
                        logger.warning("label_email failed for %s: %s", email["id"], exc)
                        label_status = "error"
                db.add(ActionLog(
                    user=user_id, action="label",
                    email_id=email["id"], email_subject=email["subject"],
                    label=label_name, status=label_status,
                ))
                db.commit()

                if cls.get("should_archive"):
                    status = "dry_run" if dry_run else "success"
                    if not dry_run:
                        provider.archive_email(email["id"])
                    log = ActionLog(
                        user=user_id, action="archive",
                        email_id=email["id"], email_subject=email["subject"],
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

            saved_groups[name] = {
                "group_id": result["group_id"],
                "emails": len(email_ids),
                "archived": sum(1 for _, c in data["emails"] if c.get("should_archive")),
                "action": result["action"],
                "summary": "",
            }
    finally:
        db.close()

    # ── Step 6: incremental group summary update ──────────────────────────────
    client = OpenAI()
    summary_blocks = []
    for name, info in saved_groups.items():
        existing_summary = existing_groups.get(name, "")
        new_email_lines = [
            f"- Subject: {e['subject']} | From: {e.get('from', '')} | {e.get('snippet', '')[:150]}"
            for e, _ in groups_to_save[name]["emails"]
        ]
        block = f"Group: {name}\n"
        if existing_summary:
            block += f"Current summary: {existing_summary}\n"
        block += "New emails:\n" + "\n".join(new_email_lines)
        summary_blocks.append(block)

    if summary_blocks:
        prompt = (
            "For each group, write a 1-2 sentence summary.\n"
            "If a current summary exists, update it to reflect the new emails — "
            "capture any status changes (resolved, confirmed, escalated, etc.).\n"
            "If no current summary, write one from the new emails.\n\n"
            + "\n\n---\n\n".join(summary_blocks)
            + '\n\nReturn JSON: {"summaries": [{"group": "...", "summary": "..."}]}'
        )
        try:
            resp = metrics.chat(
                client,
                operation="summarize",
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Update email group summaries concisely. Return only valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                max_tokens=600,
                temperature=0,
            )
            for item in json.loads(resp.choices[0].message.content).get("summaries", []):
                gname = item.get("group", "")
                summary = item.get("summary", "")
                if gname in saved_groups and summary:
                    saved_groups[gname]["summary"] = summary
                    update_group(saved_groups[gname]["group_id"], {"summary": summary})
        except Exception as exc:
            logger.warning("Incremental summary update failed: %s", exc)

    metrics.log_summary(
        label=f"finalize_email_processing ({len(emails)} emails, {len(saved_groups)} groups)",
        pipeline_stats={"processed": len(emails), "groups": len(saved_groups), "archived": len(archived)},
    )

    result = {
        "scanned": total_scanned,
        "already_processed": already_processed,
        "processed": len(emails),
        "grouped": sum(g["emails"] for g in saved_groups.values()),
        "archived": len(archived),
        "groups": saved_groups,
        "archived_emails": [{"subject": a["subject"], "log_id": a["log_id"]} for a in archived],
        "needs_attention": needs_attention,
        "api_metrics": metrics.get_summary(),
    }

    if tool_context and len(emails) > 0:
        from datetime import datetime
        tool_context.state["interaction_history"] = tool_context.state.get("interaction_history", []) + [{
            "action": "organize",
            "scanned": total_scanned,
            "processed": len(emails),
            "groups": len(saved_groups),
            "archived": len(archived),
            "timestamp": datetime.utcnow().isoformat(),
        }]
        tool_context.state["emails_processed_total"] = (
            tool_context.state.get("emails_processed_total", 0) + len(emails)
        )

    return result


def batch_process_emails(
    max_results: int = 20,
    tool_context: ToolContext = None,
) -> dict:
    """Legacy single-call pipeline: pre-process + vector clustering + finalize.

    Kept for backwards compatibility. Runs the full pipeline synchronously without
    delegating to email_grouping_agent. Equivalent to GROUPING_MODE=vector.

    Args:
        max_results: Max emails to fetch (capped at 20).
        tool_context: Injected by ADK — do not pass manually.
    """
    import os
    orig_mode = os.environ.get("GROUPING_MODE")
    os.environ["GROUPING_MODE"] = "vector"
    try:
        pre = pre_process_emails(max_results=max_results, tool_context=tool_context)
        if pre.get("remaining", 0) == 0 and pre.get("processed", pre.get("scanned", 0)) == 0:
            return pre
        return finalize_email_processing(tool_context=tool_context)
    finally:
        if orig_mode is None:
            os.environ.pop("GROUPING_MODE", None)
        else:
            os.environ["GROUPING_MODE"] = orig_mode


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
