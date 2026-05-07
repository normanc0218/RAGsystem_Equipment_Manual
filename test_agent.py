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
                "Fetch my emails, group related ones by project, "
                "archive promotional emails, summarize each group, "
                "then show me the action log."
            ))],
        ),
    ):
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    print(part.text)


asyncio.run(main())
