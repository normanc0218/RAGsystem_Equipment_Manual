import asyncio
import logging
import os
from datetime import datetime

from fastapi import APIRouter, Request
from slack_bolt import App
from slack_bolt.adapter.fastapi import SlackRequestHandler

logger = logging.getLogger(__name__)

app = App(
    token=os.getenv("SLACK_BOT_TOKEN"),
    signing_secret=os.getenv("SLACK_SIGNING_SECRET"),
)
handler = SlackRequestHandler(app)
router = APIRouter()


# ── FastAPI mount ─────────────────────────────────────────────────────────────

@router.post("/slack/events")
async def slack_events(req: Request):
    return await handler.handle(req)


# ── ADK runner ────────────────────────────────────────────────────────────────

async def _run_agent_async(task: str, user_id: str) -> str:
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types
    from email_agent import root_agent

    session_service = InMemorySessionService()
    runner = Runner(agent=root_agent, app_name="email_agent", session_service=session_service)

    session = await session_service.create_session(
        app_name="email_agent",
        user_id=user_id,
        state={
            "user_id": user_id,
            "dry_run": os.getenv("DRY_RUN", "true").lower() == "true",
        },
    )

    response_parts: list[str] = []
    for event in runner.run(
        user_id=user_id,
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=task)]),
    ):
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    response_parts.append(part.text)

    return "".join(response_parts) or "Agent completed with no response."


def _run_agent(task: str, user_id: str) -> str:
    return asyncio.run(_run_agent_async(task, user_id))


# ── /organize ─────────────────────────────────────────────────────────────────

@app.command("/organize")
def handle_organize(ack, command, client, respond):
    ack()
    user_id = command["user_id"]
    channel_id = command["channel_id"]
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    mode_note = " *(DRY RUN — no changes written to Gmail)*" if dry_run else ""

    respond(text=":robot_face: Agent is analysing your inbox… this may take a moment.")

    try:
        result = _run_agent(
            f"Run the master inbox workflow: use LabelSyncAgent if needed, classify unprocessed emails, "
            f"group them, archive promotions, and summarize the results. Include action log IDs for undo if needed.{mode_note}",
            user_id,
        )
        client.chat_postMessage(channel=channel_id, text=result)
    except Exception as exc:
        logger.exception("Error in /organize")
        respond(text=f":x: Agent error: {exc}")


# ── /digest ───────────────────────────────────────────────────────────────────

@app.command("/digest")
def handle_digest(ack, command, client, respond):
    ack()
    user_id = command["user_id"]
    channel_id = command["channel_id"]

    respond(text=":inbox_tray: Generating digest…")

    try:
        result = _run_agent(
            "Use DigestAgent to build a concise daily digest of current project groups, "
            "their summaries, email counts, and any urgent items needing my attention.",
            user_id,
        )
        client.chat_postMessage(
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
        respond(text=f":x: Digest error: {exc}")


# ── /undo ─────────────────────────────────────────────────────────────────────

@app.command("/undo")
def handle_undo(ack, command, respond):
    ack()
    text = command.get("text", "").strip()

    if not text.isdigit():
        respond(text="Usage: `/undo <action_log_id>`\nExample: `/undo 5`")
        return

    log_id = int(text)
    user_id = command["user_id"]

    try:
        result = _run_agent(f"Undo action log entry #{log_id}.", user_id)
        respond(text=result)
    except Exception as exc:
        logger.exception("Error in /undo")
        respond(text=f":x: Undo error: {exc}")
