"""
Email Agent — FastAPI entry point.

Start with:  uvicorn app_main:api --reload --port 8000
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


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("Database initialised.")
    yield


# ── App ───────────────────────────────────────────────────────────────────────

api = FastAPI(title="Email Agent", version="0.1.0", lifespan=lifespan)
api.include_router(slack_router)


# ── Health ────────────────────────────────────────────────────────────────────

@api.get("/health")
def health():
    return {
        "status": "ok",
        "dry_run": os.getenv("DRY_RUN", "true"),
        "email_provider": os.getenv("EMAIL_PROVIDER", "gmail"),
    }


# ── Gmail OAuth ───────────────────────────────────────────────────────────────

def _make_flow():
    from google_auth_oauthlib.flow import Flow

    redirect_uri = os.getenv("GMAIL_REDIRECT_URI", REDIRECT_URI_DEFAULT)
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
    """Redirect user to Google consent screen."""
    flow = _make_flow()
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    return RedirectResponse(auth_url)


@api.get("/auth/callback")
def auth_callback(code: str):
    """Exchange auth code for tokens and save to gmail_token.json."""
    flow = _make_flow()
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


# ── Action Logs ───────────────────────────────────────────────────────────────

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
        }
        for log in logs
    ]
