import logging
import os
from typing import Any, Dict

from fastapi import APIRouter, Request
from slack_bolt import App
from slack_bolt.adapter.fastapi import SlackRequestHandler

logger = logging.getLogger(__name__)

# ── Slack app ─────────────────────────────────────────────────────────────────

app = App(
    token=os.getenv("SLACK_BOT_TOKEN"),
    signing_secret=os.getenv("SLACK_SIGNING_SECRET"),
)
handler = SlackRequestHandler(app)
router = APIRouter()

# In-memory store: slack user_id → pending classification plan.
# Good enough for MVP; replace with Redis or DB for multi-instance deploys.
_pending_plans: Dict[str, Dict[str, Any]] = {}


# ── FastAPI mount point ───────────────────────────────────────────────────────

@router.post("/slack/events")
async def slack_events(req: Request):
    return await handler.handle(req)


# ── /organise ─────────────────────────────────────────────────────────────────

@app.command("/organise")
def handle_organise(ack, command, client, respond):
    ack()
    user_id = command["user_id"]
    channel_id = command["channel_id"]

    respond(text=":inbox_tray: Fetching your emails and generating a plan…")

    try:
        from app.services.ai_service import classify_emails
        from app.services.email_provider import get_email_provider

        provider = get_email_provider()
        emails = provider.fetch_emails(max_results=50)

        if not emails:
            respond(text="No emails found in your inbox.")
            return

        plan = classify_emails(emails)

        # Store plan so the Confirm handler can retrieve it without re-fetching
        _pending_plans[user_id] = {
            "plan": plan,
            "emails_by_id": {e["id"]: e for e in emails},
        }

        client.chat_postMessage(
            channel=channel_id,
            blocks=_build_plan_blocks(plan),
            text="Email organisation plan ready — please review and confirm.",
        )

    except Exception as exc:
        logger.exception("Error in /organise")
        respond(text=f":x: Error: {exc}")


# ── /digest ───────────────────────────────────────────────────────────────────

@app.command("/digest")
def handle_digest(ack, command, client):
    ack()
    _send_daily_digest(client, command["channel_id"])


def _send_daily_digest(slack_client, channel: str) -> None:
    """Fetch inbox, classify with AI, and post a digest summary to Slack."""
    from app.services.ai_service import classify_emails
    from app.services.email_provider import get_email_provider

    try:
        provider = get_email_provider()
        emails = provider.fetch_emails(max_results=50)

        if not emails:
            slack_client.chat_postMessage(
                channel=channel,
                text="*Daily Email Digest*: Inbox is empty — nothing to report.",
            )
            return

        plan = classify_emails(emails)
        summary = plan.get("summary", "No summary available.")
        items = plan.get("emails", [])

        archive_n = sum(1 for e in items if e.get("suggested_action") == "archive")
        label_n = sum(1 for e in items if e.get("suggested_action") == "label")
        keep_n = sum(1 for e in items if e.get("suggested_action") == "keep")

        from datetime import datetime

        slack_client.chat_postMessage(
            channel=channel,
            blocks=[
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"Email Digest — {datetime.now().strftime('%b %d, %Y')}",
                    },
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Summary:* {summary}"},
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Emails analysed:* {len(items)}"},
                        {
                            "type": "mrkdwn",
                            "text": f"*Archive:* {archive_n}  |  *Label:* {label_n}  |  *Keep:* {keep_n}",
                        },
                    ],
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Run `/organise` to review the full plan and confirm actions.",
                    },
                },
            ],
        )
    except Exception:
        logger.exception("Digest failed")
        slack_client.chat_postMessage(
            channel=channel,
            text=":warning: Digest encountered an error. Check server logs.",
        )


# ── Button: Confirm ───────────────────────────────────────────────────────────

@app.action("confirm_plan")
def handle_confirm(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    channel_id = body["channel"]["id"]
    message_ts = body["message"]["ts"]

    pending = _pending_plans.pop(user_id, None)
    if not pending:
        client.chat_postMessage(
            channel=channel_id,
            text=":warning: No pending plan found. Run `/organise` again.",
        )
        return

    _execute_plan(pending["plan"], user_id, channel_id, client)

    # Replace the plan message with a compact confirmed state
    client.chat_update(
        channel=channel_id,
        ts=message_ts,
        text="Plan executed.",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": ":white_check_mark: *Plan confirmed and executed.* See results above.",
                },
            }
        ],
    )


# ── Button: Cancel ────────────────────────────────────────────────────────────

@app.action("cancel_plan")
def handle_cancel(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    _pending_plans.pop(user_id, None)

    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text="Plan cancelled.",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": ":x: *Plan cancelled.* No changes were made.",
                },
            }
        ],
    )


# ── Block Kit builder ─────────────────────────────────────────────────────────

_ACTION_EMOJI = {"archive": ":file_cabinet:", "label": ":label:", "keep": ":white_check_mark:"}


def _build_plan_blocks(plan: Dict[str, Any]) -> list:
    items = plan.get("emails", [])
    summary = plan.get("summary", "")

    blocks: list = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Email Organisation Plan"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*AI Summary:* {summary}"},
        },
        {"type": "divider"},
    ]

    # Slack has a 50-block limit; show up to 20 email rows safely
    for item in items[:20]:
        action = item.get("suggested_action", "keep")
        emoji = _ACTION_EMOJI.get(action, ":grey_question:")
        label_suffix = f" → `{item['label']}`" if item.get("label") else ""
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{emoji} *{action.upper()}*{label_suffix}\n"
                        f"*Subject:* {item.get('subject', '(no subject)')}\n"
                        f"_{item.get('reason', '')}_"
                    ),
                },
            }
        )

    if len(items) > 20:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"_…and {len(items) - 20} more emails not shown_",
                },
            }
        )

    blocks += [
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Confirm — Execute Plan"},
                    "style": "primary",
                    "action_id": "confirm_plan",
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Apply to all emails?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"This will apply suggested actions to *{len(items)} emails*.\n"
                                "Emails are archived, not deleted. This cannot be undone."
                            ),
                        },
                        "confirm": {"type": "plain_text", "text": "Yes, execute"},
                        "deny": {"type": "plain_text", "text": "Wait, cancel"},
                    },
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Cancel"},
                    "style": "danger",
                    "action_id": "cancel_plan",
                },
            ],
        },
    ]
    return blocks


# ── Plan execution ────────────────────────────────────────────────────────────

def _execute_plan(plan: Dict[str, Any], user_id: str, channel_id: str, client) -> None:
    """Apply the classified actions to Gmail and log every action to SQLite."""
    from app.database import SessionLocal
    from app.models.action_log import ActionLog
    from app.services.email_provider import get_email_provider

    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    provider = get_email_provider()
    db = SessionLocal()
    results: list[str] = []

    try:
        email_items = plan.get("emails", [])[:50]  # hard cap — safety rule #3

        for item in email_items:
            email_id = item["id"]
            action = item.get("suggested_action", "keep")
            label = item.get("label") or ""
            subject = item.get("subject", email_id)
            status = "dry_run" if dry_run else "success"

            try:
                if not dry_run:
                    if action == "archive":
                        provider.archive_email(email_id)
                    elif action == "label" and label:
                        provider.label_email(email_id, label)
                    # "keep" → no write call

                db.add(
                    ActionLog(
                        user=user_id,
                        action=action,
                        email_id=email_id,
                        email_subject=subject,
                        label=label or None,
                        status=status,
                    )
                )
                tag = "[DRY RUN] " if dry_run else ""
                results.append(f"{tag}{action.upper()}: {subject}")

            except Exception as exc:
                db.add(
                    ActionLog(
                        user=user_id,
                        action=action,
                        email_id=email_id,
                        email_subject=subject,
                        label=label or None,
                        status="failed",
                    )
                )
                results.append(f":warning: FAILED ({subject}): {exc}")

        db.commit()

    finally:
        db.close()

    # Post execution summary
    header = ":construction: *DRY RUN — no actual changes written to Gmail*\n" if dry_run else ""
    lines = results[:25]
    overflow = f"\n_…and {len(results) - 25} more_" if len(results) > 25 else ""
    client.chat_postMessage(
        channel=channel_id,
        text=header + "\n".join(lines) + overflow,
    )
