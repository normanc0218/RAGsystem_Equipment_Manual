import asyncio
import os
from dotenv import load_dotenv
load_dotenv()

from app.database import init_db
init_db()

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from email_agent import root_agent


async def main():
    session_service = InMemorySessionService()
    runner = Runner(agent=root_agent, app_name="email_agent", session_service=session_service)

    session = await session_service.create_session(
        app_name="email_agent",
        user_id="test_user",
        state={"user_id": "test_user", "dry_run": True},
    )

    provider = os.getenv("EMAIL_PROVIDER", "gmail")
    dry_run = os.getenv("DRY_RUN", "true")
    print(f"Running email agent | provider={provider} | dry_run={dry_run}\n")
    print("=" * 60)

    for event in runner.run(
        user_id="test_user",
        session_id=session.id,
        new_message=types.Content(
            role="user",
            parts=[types.Part(text=(
                "Run the full two-phase workflow: "
                "first sync my Gmail labels, then batch process up to 200 emails "
                "(max_results=200, random_sample=True), summarize each group, "
                "and show me the action log."
            ))],
        ),
    ):
        # Tool calls — what the agent decides to do
        if event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    print(f"\n→ TOOL CALL: {fc.name}({dict(fc.args)})")
                elif hasattr(part, "function_response") and part.function_response:
                    fr = part.function_response
                    print(f"← TOOL RESULT: {fr.name} → {str(fr.response)[:300]}")
                elif hasattr(part, "text") and part.text and event.is_final_response():
                    print(f"\n{'='*60}\nFINAL RESPONSE:\n{part.text}")


asyncio.run(main())
