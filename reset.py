"""
Reset script — clears all persistent state for a clean test run.

Clears:
  - Firestore: email_groups + email_summaries
  - SQLite: action_logs (email_agent.db)
  - ADK session DB
"""
import os
from dotenv import load_dotenv
load_dotenv()

from agent_service.email_agent.database import Base, engine, init_db
from agent_service.email_agent.services.firestore_service import delete_all_groups, delete_all_summaries

print("Resetting all state...\n")

# ── Firestore ─────────────────────────────────────────────────────────────────
g = delete_all_groups()
s = delete_all_summaries()
print(f"  Firestore: deleted {g} groups, {s} summaries")

# ── SQLite ────────────────────────────────────────────────────────────────────
Base.metadata.drop_all(bind=engine)
init_db()
print("  SQLite: action_logs table reset")

# ── ADK session DB ────────────────────────────────────────────────────────────
adk_db = "email_agent_sessions.db"
if os.path.exists(adk_db):
    os.remove(adk_db)
    print("  ADK: email_agent_sessions.db deleted")
else:
    print("  ADK: no session.db found")

print("\nDone. Ready for a fresh run.")
