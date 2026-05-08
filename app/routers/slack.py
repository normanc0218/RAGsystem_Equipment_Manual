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


async def _run_agent(task: str, user_id: str) -> str:
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

    response_parts: list[str] = []
    async for event in _runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=task)]),
    ):
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    response_parts.append(part.text)

    return "".join(response_parts) or "Agent completed with no response."


# ── FastAPI mount ─────────────────────────────────────────────────────────────

@router.post("/slack/events")
async def slack_events(req: Request):
    return await handler.handle(req)


# ── /organize ─────────────────────────────────────────────────────────────────

@app.command("/organize")
async def handle_organize(ack, command, client, respond):
    await ack()
    user_id = command["user_id"]
    channel_id = command["channel_id"]
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    mode_note = " *(DRY RUN — no changes written to Gmail)*" if dry_run else ""

    await respond(text=":robot_face: Agent is analysing your inbox… this may take a moment.")

    try:
        result = await _run_agent(
            f"Run the master inbox workflow: use LabelSyncAgent if needed, classify unprocessed emails, "
            f"group them, archive promotions, and summarize the results. Include action log IDs for undo if needed.{mode_note}",
            user_id,
        )
        await client.chat_postMessage(channel=channel_id, text=result)
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
        result = await _run_agent(
            "Use DigestAgent to build a concise daily digest of current project groups, "
            "their summaries, email counts, and any urgent items needing my attention.",
            user_id,
        )
        await client.chat_postMessage(
            channel=channel_id,
            blocks=[
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": f"Email Digest — {datetime.now().strftime('%b %d, %Y')}"},
                },
                {"type": "section", "text": {"type": "mrkdwn", "text": result}},
            ],
            text=result,
        )
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
