import asyncio
import os

from dotenv import load_dotenv
load_dotenv()

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from .email_agent import root_agent
from .email_agent.database import init_db

init_db()

APP_NAME = "email_agent"

# ===== PART 1: Initialize Session Service =====
# InMemorySessionService avoids SQLite optimistic-lock conflicts during the
# multi-step ORGANIZE flow (4 sub-agent transfers each writing session state).
# All durable state lives in Firestore; sessions only carry cheap per-request
# context (user_id, dry_run) that is recreated on each Slack command.
session_service = InMemorySessionService()

# ===== PART 2: Define Initial State =====
def make_initial_state(user_id: str = "unknown", user_name: str = "") -> dict:
    return {
        "user_id": user_id,
        "user_name": user_name,
        "dry_run": os.getenv("DRY_RUN", "true").lower() == "true",
        "interaction_history": [],   # [{action, processed, groups, archived, timestamp}]
        "last_sync_time": None,      # ISO timestamp of last Gmail label sync
        "emails_processed_total": 0, # running count across all sessions
    }

# ===== PART 3: Agent Runner Setup =====
runner = Runner(
    agent=root_agent,
    app_name=APP_NAME,
    session_service=session_service,
)


async def main_async():
    USER_ID = os.getenv("ADK_USER_ID", "local_user")

    # ===== Session: find or create =====
    existing = await session_service.list_sessions(app_name=APP_NAME, user_id=USER_ID)
    if existing and existing.sessions:
        session_id = existing.sessions[0].id
        print(f"Continuing session: {session_id}")
    else:
        session = await session_service.create_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            state=make_initial_state(user_id=USER_ID),
        )
        session_id = session.id
        print(f"Created session: {session_id}")

    session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
    print(f"\nSession state: {session.state}")
    print(f"Email Agent | dry_run={os.getenv('DRY_RUN', 'true')}")
    print("Type 'exit' to quit.\n" + "=" * 60)

    # ===== Interactive loop =====
    while True:
        user_input = input("\nYou: ").strip()
        if user_input.lower() in ("exit", "quit"):
            print("Goodbye!")
            break
        if not user_input:
            continue

        async for event in runner.run_async(
            user_id=USER_ID,
            session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=user_input)]),
        ):
            if not event.content or not event.content.parts:
                continue
            for part in event.content.parts:
                if hasattr(part, "function_call") and part.function_call:
                    print(f"\n→ TOOL: {part.function_call.name}({dict(part.function_call.args)})")
                elif hasattr(part, "function_response") and part.function_response:
                    print(f"← RESULT: {part.function_response.name} → {str(part.function_response.response)[:300]}")
                elif hasattr(part, "text") and part.text and event.is_final_response():
                    print(f"\n{'=' * 60}\n{part.text}")


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
