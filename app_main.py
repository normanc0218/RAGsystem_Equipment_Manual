"""
Email Agent — FastAPI entry point.

Start with:  uvicorn app_main:api --reload --port 8000

Databases:
  email_agent.db  — action_logs only (SQLite)
  Firestore       — email_groups + email_summaries
"""
import json
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db, init_db
from app.models.action_log import ActionLog
from app.routers.slack import router as slack_router

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
REDIRECT_URI_DEFAULT = "http://localhost:8000/auth/callback"

_pending_flows: dict = {}


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("SQLite action_logs initialised.")
    yield


# ── App ───────────────────────────────────────────────────────────────────────

api = FastAPI(title="Email Agent", version="0.3.0", lifespan=lifespan)
api.include_router(slack_router)


# ── Health ────────────────────────────────────────────────────────────────────

@api.get("/health")
def health():
    return {
        "status": "ok",
        "dry_run": os.getenv("DRY_RUN", "true"),
        "email_provider": os.getenv("EMAIL_PROVIDER", "gmail"),
        "firestore_project": os.getenv("GOOGLE_CLOUD_PROJECT", "not set"),
    }


# ── Gmail OAuth ───────────────────────────────────────────────────────────────

def _make_flow():
    from google_auth_oauthlib.flow import Flow

    redirect_uri = os.getenv("GMAIL_REDIRECT_URI", REDIRECT_URI_DEFAULT)
    secrets_file = os.getenv("GMAIL_CLIENT_SECRETS_FILE", "gmail-client-secret.json")

    if os.path.exists(secrets_file):
        flow = Flow.from_client_secrets_file(secrets_file, scopes=GMAIL_SCOPES)
    else:
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": os.getenv("GMAIL_CLIENT_ID"),
                    "client_secret": os.getenv("GMAIL_CLIENT_SECRET"),
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [redirect_uri],
                }
            },
            scopes=GMAIL_SCOPES,
        )

    flow.redirect_uri = redirect_uri
    return flow


@api.get("/auth/login")
def auth_login():
    flow = _make_flow()
    auth_url, state = flow.authorization_url(prompt="consent", access_type="offline")
    _pending_flows[state] = flow
    return RedirectResponse(auth_url)


@api.get("/auth/callback")
def auth_callback(code: str, state: str = ""):
    flow = _pending_flows.pop(state, None) or _make_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials
    with open("gmail_token.json", "w") as fh:
        json.dump(
            {
                "token": creds.token,
                "refresh_token": creds.refresh_token,
                "token_uri": creds.token_uri,
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "scopes": list(creds.scopes or []),
            },
            fh,
            indent=2,
        )
    return {"status": "Gmail authenticated. You can close this tab and return to Slack."}


# ── Action Logs (SQLite) ──────────────────────────────────────────────────────

@api.get("/logs")
def get_logs(limit: int = 100, db: Session = Depends(get_db)):
    logs = (
        db.query(ActionLog)
        .order_by(ActionLog.timestamp.desc())
        .limit(min(limit, 500))
        .all()
    )
    return [
        {
            "id": log.id,
            "timestamp": log.timestamp.isoformat(),
            "user": log.user,
            "action": log.action,
            "email_id": log.email_id,
            "email_subject": log.email_subject,
            "label": log.label,
            "status": log.status,
            "undo_status": log.undo_status,
            "undone_at": log.undone_at.isoformat() if log.undone_at else None,
        }
        for log in logs
    ]


# ── Project Groups (Firestore) ────────────────────────────────────────────────

@api.get("/groups")
def get_groups():
    from app.services.firestore_service import list_groups
    return list_groups()
