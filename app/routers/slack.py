import asyncio
import logging
import os
from datetime import datetime

from fastapi import APIRouter, Request
from google.genai import types
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler

from agent_service.main import APP_NAME, make_initial_state, runner as _runner, session_service as _session_service

logger = logging.getLogger(__name__)

app = AsyncApp(
    token=os.getenv("SLACK_BOT_TOKEN"),
    signing_secret=os.getenv("SLACK_SIGNING_SECRET"),
)
handler = AsyncSlackRequestHandler(app)
router = APIRouter()


async def _get_slack_display_name(user_id: str) -> str:
    try:
        from slack_sdk.web.async_client import AsyncWebClient
        client = AsyncWebClient(token=os.getenv("SLACK_BOT_TOKEN"))
        resp = await client.users_info(user=user_id)
        profile = resp["user"].get("profile", {})
        return profile.get("display_name") or profile.get("real_name") or ""
    except Exception:
        return ""


_user_locks: dict[str, asyncio.Lock] = {}


def _get_user_lock(user_id: str) -> asyncio.Lock:
    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()
    return _user_locks[user_id]


async def _run_agent(task: str, user_id: str) -> str:
    async with _get_user_lock(user_id):
        existing = await _session_service.list_sessions(app_name=APP_NAME, user_id=user_id)
        if existing and existing.sessions:
            session_id = existing.sessions[0].id
        else:
            user_name = await _get_slack_display_name(user_id)
            session = await _session_service.create_session(
                app_name=APP_NAME,
                user_id=user_id,
                state=make_initial_state(user_id=user_id, user_name=user_name),
            )
            session_id = session.id

        last_response = ""
        async for event in _runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=task)]),
        ):
            if event.is_final_response() and event.content and event.content.parts:
                for part in event.content.parts:
                    if hasattr(part, "text") and part.text:
                        last_response = part.text  # last final response wins

        return last_response or "Agent completed with no response."


# ── FastAPI mount ─────────────────────────────────────────────────────────────

@router.post("/slack/events")
async def slack_events(req: Request):
    return await handler.handle(req)


@router.post("/slack/interactions")
async def slack_interactions(req: Request):
    return await handler.handle(req)


# ── Label setup helpers ───────────────────────────────────────────────────────

def _build_label_modal(
    label_name: str,
    options: list[str],
    remaining: list[str],
    channel_id: str = "",
    user_id: str = "",
    run_agent_after: bool = False,
) -> dict:
    """Build a modal view for one empty label. Chains to the next via private_metadata."""
    import json as _json
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":label: *Label:* `{label_name}`\nWhat kind of emails should I sort into this label?",
            },
        },
    ]
    if options:
        blocks.append({
            "type": "input",
            "block_id": "option_block",
            "optional": True,
            "element": {
                "type": "radio_buttons",
                "action_id": "option_input",
                "options": [
                    {"text": {"type": "plain_text", "text": opt}, "value": opt}
                    for opt in options
                ],
            },
            "label": {"type": "plain_text", "text": "Choose a category"},
        })
    blocks.append({
        "type": "input",
        "block_id": "custom_block",
        "optional": True,
        "element": {
            "type": "plain_text_input",
            "action_id": "custom_input",
            "multiline": True,
            "placeholder": {
                "type": "plain_text",
                "text": "e.g. Fault alarms and error reports from Siemens equipment",
            },
        },
        "label": {"type": "plain_text", "text": "Or describe it yourself"},
    })

    submit_text = f"Save & Next ({len(remaining)} more)" if remaining else "Save & Organise"
    return {
        "type": "modal",
        "callback_id": "label_setup_modal",
        "private_metadata": _json.dumps({
            "current": label_name,
            "remaining": remaining,
            "channel_id": channel_id,
            "user_id": user_id,
            "run_agent_after": run_agent_after,
        }),
        "title": {"type": "plain_text", "text": "Set up label"},
        "submit": {"type": "plain_text", "text": submit_text},
        "close": {"type": "plain_text", "text": "Skip"},
        "blocks": blocks,
    }


def _format_organize_result(text: str) -> str:
    """If the agent returned raw JSON, render it as clean Slack mrkdwn."""
    import json, re
    match = re.search(r"\{[\s\S]+\}", text)
    if not match:
        return text
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return text

    lines = []
    lines.append("*📊 Summary*")
    lines.append(
        f"• {data.get('processed', 0)} emails processed → "
        f"{len(data.get('groups', {}))} groups  |  {data.get('archived', 0)} archived"
    )

    attention = data.get("needs_attention", [])
    lines.append("\n*⚠️ Needs Attention*")
    if attention:
        for item in attention[:3]:
            lines.append(f"• {item.get('subject', '')} — {item.get('reason', '')}")
    else:
        lines.append("• None")

    lines.append("\n*📁 Groups*")
    for name, info in list(data.get("groups", {}).items())[:8]:
        summary = info.get("summary", "")
        short = " ".join(summary.split()[:10]) + ("…" if len(summary.split()) > 10 else "")
        lines.append(f"• *{name}* ({info.get('emails', 0)} emails) — {short}")

    return "\n".join(lines)


def _parse_organize_report(text: str) -> dict:
    """Parse a Slack-style organize report into structured sections."""
    import re

    lines = [line.rstrip() for line in text.splitlines()]
    report = {"summary": [], "needs_attention": [], "groups": [], "raw": text}
    section = None

    heading_map = {
        re.compile(r"^\*?\s*📊\s*Summary\s*\*?$", re.IGNORECASE): "summary",
        re.compile(r"^\*?\s*⚠️\s*Needs\s+Attention\s*\*?$", re.IGNORECASE): "needs_attention",
        re.compile(r"^\*?\s*📁\s*Groups\s*\*?$", re.IGNORECASE): "groups",
        re.compile(r"^\*?\s*📦\s*Archived\s*\*?$", re.IGNORECASE): "archived",
    }

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        matched = False
        for pattern, name in heading_map.items():
            if pattern.match(stripped):
                section = name
                if name == "archived":
                    report.setdefault("archived", [])
                matched = True
                break
        if matched:
            continue

        if stripped.startswith("•") or stripped.startswith("-"):
            content = stripped[1:].strip()
        else:
            content = stripped

        if section:
            if section == "summary" and not content.startswith("•"):
                report[section].append(content)
            else:
                report.setdefault(section, []).append(content)
        else:
            report.setdefault("other", []).append(content)

    # If the parser found nothing useful, preserve raw text in summary for fallback.
    if not any(report.get(k) for k in ("summary", "needs_attention", "groups")):
        report["summary"] = [text.strip()]

    return report


def _create_mrkdwn_blocks(title: str, lines: list[str]) -> list[dict]:
    if not lines:
        return []
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}}]
    text = "\n".join(f"• {line}" for line in lines)
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
    return blocks


def _chunked_sections(title: str, lines: list[str], max_chars: int = 2800) -> list[dict]:
    blocks = []
    if not lines:
        return blocks
    chunk = []
    current = 0
    for line in lines:
        if current + len(line) + 1 > max_chars and chunk:
            blocks.extend(_create_mrkdwn_blocks(title, chunk))
            blocks.append(_DIVIDER)
            chunk = []
            current = 0
        chunk.append(line)
        current += len(line) + 1
    if chunk:
        blocks.extend(_create_mrkdwn_blocks(title, chunk))
    return blocks


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


_DIVIDER = {"type": "divider"}


async def _post_digest_result(client, channel_id: str, data: dict) -> None:
    """Render a structured daily digest as Slack Block Kit."""
    group_count = data.get("group_count", 0)
    total_emails = data.get("total_emails", 0)
    groups = data.get("groups", [])

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📧 Daily Digest — {datetime.now().strftime('%b %d, %Y')}"},
        },
        _section(f"*{group_count} groups*  ·  *{total_emails} emails* organised"),
        _DIVIDER,
    ]

    if not groups:
        blocks.append(_section("No groups yet — run `/organize` to get started."))
        await client.chat_postMessage(channel=channel_id, blocks=blocks, text="Daily Digest")
        return

    # ── split groups into attention vs normal ────────────────────────────────
    attention_keywords = ("urgent", "overdue", "required", "action", "overdue", "fault",
                          "alarm", "escalat", "safety", "deadline", "payment", "error")
    attention, normal = [], []
    for g in groups:
        summary_lower = (g.get("summary") or "").lower()
        if any(kw in summary_lower for kw in attention_keywords):
            attention.append(g)
        else:
            normal.append(g)

    # ── needs attention section ───────────────────────────────────────────────
    if attention:
        lines = ["*⚠️ Needs Attention*"]
        for g in attention:
            summary = (g.get("summary") or "").strip()
            short = (summary[:80] + "…") if len(summary) > 80 else summary
            count = g.get("email_count", 0)
            lines.append(f"• *{g['name']}* ({count}) — _{short}_")
        blocks.append(_section("\n".join(lines)))
        blocks.append(_DIVIDER)

    # ── all groups section ────────────────────────────────────────────────────
    group_lines = ["*📁 Groups* _(sorted by recent activity)_"]
    for g in normal + attention:
        summary = (g.get("summary") or "").strip()
        short = (summary[:80] + "…") if len(summary) > 80 else summary
        count = g.get("email_count", 0)
        line = f"• *{g['name']}* ({count} email{'s' if count != 1 else ''})"
        if short:
            line += f"\n  _{short}_"
        group_lines.append(line)

    current, chunk_lines = 0, []
    for line in group_lines:
        if current + len(line) + 1 > 2900:
            blocks.append(_section("\n".join(chunk_lines)))
            chunk_lines, current = [], 0
        chunk_lines.append(line)
        current += len(line) + 1
    if chunk_lines:
        blocks.append(_section("\n".join(chunk_lines)))

    await client.chat_postMessage(
        channel=channel_id, blocks=blocks,
        text=f"Daily Digest — {group_count} groups, {total_emails} emails",
    )


async def _post_organize_result(client, channel_id: str, result: str) -> None:
    """Post the organise report as Block Kit. Falls back to formatting raw JSON if needed."""
    formatted = _format_organize_result(result)
    parsed = _parse_organize_report(formatted)

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"✉️ Inbox Organised — {datetime.now().strftime('%b %d, %Y')}"},
        }
    ]

    if parsed["summary"]:
        blocks.extend(_create_mrkdwn_blocks("📊 Summary", parsed["summary"]))
        blocks.append(_DIVIDER)

    if parsed["needs_attention"]:
        blocks.extend(_create_mrkdwn_blocks("⚠️ Needs Attention", parsed["needs_attention"]))
        blocks.append(_DIVIDER)
    else:
        blocks.append(_section("*⚠️ Needs Attention*\n• None"))
        blocks.append(_DIVIDER)

    if parsed["groups"]:
        blocks.extend(_chunked_sections("📁 Groups", parsed["groups"]))
    else:
        blocks.append(_section("*📁 Groups*\n• None"))

    # Fallback: if parsing failed and blocks are minimal, include raw text as a final section.
    if len(blocks) <= 2:
        blocks.append(_section(formatted))

    await client.chat_postMessage(channel=channel_id, blocks=blocks, text=formatted[:500])


async def _check_empty_labels(client, user_id: str, channel_id: str, run_agent_after: bool = False) -> bool:
    """Check for empty labels. Posts ephemeral prompt if any found. Returns True if found."""
    import asyncio, json
    from agent_service.email_agent.services.label_setup_service import find_empty_user_labels

    try:
        empty_labels = await asyncio.to_thread(find_empty_user_labels)
    except Exception as exc:
        logger.warning("Could not check empty labels: %s", exc)
        return False

    if not empty_labels:
        return False

    label_names = [lbl["name"] for lbl in empty_labels[:3]]
    count = len(label_names)

    await client.chat_postEphemeral(
        channel=channel_id,
        user=user_id,
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":label: I found *{count} empty label{'s' if count > 1 else ''}* "
                        f"with no sorting rules yet.\n"
                        f"Set {'them' if count > 1 else 'it'} up first so I can organise your emails correctly."
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": f"Set up & Organise →"},
                        "value": json.dumps({
                            "labels": label_names,
                            "run_agent_after": run_agent_after,
                        }),
                        "action_id": "setup_empty_labels",
                        "style": "primary",
                    }
                ],
            },
        ],
        text=f"Found {count} empty label(s) that need setup before organising.",
    )
    return True


# ── Label setup action handlers ───────────────────────────────────────────────

@app.action("setup_empty_labels")
async def handle_setup_labels_button(ack, body, client):
    """Button click → open modal for the first empty label."""
    await ack()
    import asyncio, json
    from agent_service.email_agent.services.label_setup_service import generate_label_options

    payload = json.loads(body["actions"][0]["value"])
    label_names = payload["labels"]
    run_agent_after = payload.get("run_agent_after", False)
    channel_id = body.get("channel", {}).get("id", "")
    user_id = body["user"]["id"]

    first, remaining = label_names[0], label_names[1:]
    try:
        options = await asyncio.to_thread(generate_label_options, first)
    except Exception:
        options = []

    await client.views_open(
        trigger_id=body["trigger_id"],
        view=_build_label_modal(first, options, remaining, channel_id, user_id, run_agent_after),
    )


@app.view("label_setup_modal")
async def handle_label_setup_submit(ack, body, client):
    """Modal submit → seed Firestore group; chain to next label or run agent when done."""
    import asyncio, json
    from agent_service.email_agent.services.label_setup_service import (
        seed_group_from_description, generate_label_options,
    )

    metadata = json.loads(body["view"]["private_metadata"])
    label_name = metadata["current"]
    remaining = metadata["remaining"]
    channel_id = metadata.get("channel_id", "")
    user_id = metadata.get("user_id", "") or body["user"]["id"]
    run_agent_after = metadata.get("run_agent_after", False)

    values = body["view"]["state"]["values"]
    custom = (values.get("custom_block", {}).get("custom_input", {}).get("value") or "").strip()
    selected = ((values.get("option_block", {}).get("option_input", {}) or {}).get("selected_option") or {}).get("value", "")
    description = custom or selected

    if description:
        try:
            await asyncio.to_thread(seed_group_from_description, label_name, description)
        except Exception:
            logger.exception("Error seeding label group '%s'", label_name)

    if remaining:
        next_label, next_remaining = remaining[0], remaining[1:]
        try:
            options = await asyncio.to_thread(generate_label_options, next_label)
        except Exception:
            options = []
        await ack(response_action="update", view=_build_label_modal(
            next_label, options, next_remaining, channel_id, user_id, run_agent_after,
        ))
    else:
        await ack()
        if run_agent_after and channel_id:
            dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
            mode_note = " *(DRY RUN — no changes written to Gmail)*" if dry_run else ""
            await client.chat_postMessage(
                channel=channel_id,
                text=":robot_face: Labels set up! Running /organize now…",
            )
            try:
                result = await _run_agent(
                    f"Run the master inbox workflow: use LabelSyncAgent if needed, classify unprocessed emails "
                    f"(max 20 emails), group them, archive promotions, and summarize the results.{mode_note}",
                    user_id,
                )
                await _post_organize_result(client, channel_id, result)
            except Exception as exc:
                logger.exception("Error running agent after label setup")
                await client.chat_postMessage(channel=channel_id, text=f":x: Agent error: {exc}")


# ── /organize ─────────────────────────────────────────────────────────────────

@app.command("/organize")
async def handle_organize(ack, command, client, respond):
    await ack()
    user_id = command["user_id"]
    channel_id = command["channel_id"]
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    mode_note = " *(DRY RUN — no changes written to Gmail)*" if dry_run else ""

    # Check empty labels first — stop and prompt user before running agent
    has_empty = await _check_empty_labels(client, user_id, channel_id, run_agent_after=True)
    if has_empty:
        return

    await respond(text=":robot_face: Analysing your inbox… this may take a moment.")
    try:
        result = await _run_agent(
            f"Run the master inbox workflow: use LabelSyncAgent if needed, classify unprocessed emails "
            f"(max 20 emails), group them, archive promotions, and summarize the results.{mode_note}",
            user_id,
        )
        await _post_organize_result(client, channel_id, result)
    except Exception as exc:
        logger.exception("Error in /organize")
        await respond(text=f":x: Agent error: {exc}")


# ── /digest ───────────────────────────────────────────────────────────────────

@app.command("/digest")
async def handle_digest(ack, command, client, respond):
    await ack()
    user_id = command["user_id"]
    channel_id = command["channel_id"]

    await respond(text=":inbox_tray: Generating digest…")

    try:
        import asyncio
        from agent_service.email_agent.tools.digest_tools import daily_digest
        data = await asyncio.to_thread(daily_digest)
        await _post_digest_result(client, channel_id, data)
    except Exception as exc:
        logger.exception("Error in /digest")
        await respond(text=f":x: Digest error: {exc}")


# ── /undo ─────────────────────────────────────────────────────────────────────

@app.command("/undo")
async def handle_undo(ack, command, respond):
    await ack()
    text = command.get("text", "").strip()

    if not text.isdigit():
        await respond(text="Usage: `/undo <action_log_id>`\nExample: `/undo 5`")
        return

    log_id = int(text)
    user_id = command["user_id"]

    try:
        result = await _run_agent(f"Undo action log entry #{log_id}.", user_id)
        await respond(text=result)
    except Exception as exc:
        logger.exception("Error in /undo")
        await respond(text=f":x: Undo error: {exc}")


# ── Direct messages & @mentions ───────────────────────────────────────────────

@app.event("message")
async def handle_dm(event, client, say):
    """Handle direct messages sent to the bot."""
    # Ignore bot messages and message edits/deletes to avoid loops
    if event.get("bot_id") or event.get("subtype"):
        return

    user_id = event.get("user")
    text = event.get("text", "").strip()
    if not user_id or not text:
        return

    try:
        result = await _run_agent(text, user_id)
        await say(text=result)
    except Exception as exc:
        logger.exception("Error handling DM")
        await say(text=f":x: Error: {exc}")


@app.event("app_mention")
async def handle_mention(event, client, say):
    """Handle @mentions in channels."""
    user_id = event.get("user")
    # Strip the @mention tag from the message text
    text = event.get("text", "")
    text = " ".join(w for w in text.split() if not w.startswith("<@")).strip()
    if not user_id or not text:
        await say(text="Hi! Try `/organize`, `/digest`, or just ask me anything.")
        return

    try:
        result = await _run_agent(text, user_id)
        await say(text=result)
    except Exception as exc:
        logger.exception("Error handling mention")
        await say(text=f":x: Error: {exc}")
