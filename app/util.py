"""
Utility routes — health, Gmail OAuth, action log inspection, and group inspection.
"""
import json
import logging
import os

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from agent_service.email_agent.database import get_db
from agent_service.email_agent.models.action_log import ActionLog

router = APIRouter()
logger = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
REDIRECT_URI_DEFAULT = "http://localhost:8000/auth/callback"

_pending_flows: dict = {}


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


# ── Health ────────────────────────────────────────────────────────────────────

@router.get("/health")
def health():
    return {
        "status": "ok",
        "dry_run": os.getenv("DRY_RUN", "true"),
        "email_provider": os.getenv("EMAIL_PROVIDER", "gmail"),
        "firestore_project": os.getenv("GOOGLE_CLOUD_PROJECT", "not set"),
    }


# ── Gmail OAuth ───────────────────────────────────────────────────────────────

@router.get("/auth/login")
def auth_login():
    flow = _make_flow()
    auth_url, state = flow.authorization_url(prompt="consent", access_type="offline")
    _pending_flows[state] = flow
    return RedirectResponse(auth_url)


@router.get("/auth/callback")
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

@router.get("/logs")
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


# ── API Metrics ───────────────────────────────────────────────────────────────

@router.get("/metrics")
def get_metrics():
    from agent_service.email_agent.services.metrics_service import metrics, _METRICS_FILE
    import json
    runs = json.loads(_METRICS_FILE.read_text()) if _METRICS_FILE.exists() else []
    if not runs:
        last = metrics.last_run
        if not last:
            return {"status": "no run recorded yet — trigger /organize first"}
        runs = [last]
    # attach computed totals to each run
    result = []
    for run in runs:
        ops = run.get("operations", {})
        result.append({
            "timestamp": run.get("timestamp"),
            "label": run.get("label"),
            "summary": {
                "total_calls": sum(v["calls"] for v in ops.values()),
                "total_tokens": sum(v["input_tokens"] + v["output_tokens"] for v in ops.values()),
                "total_cost_usd": round(sum(v["cost_usd"] for v in ops.values()), 6),
                "total_elapsed_s": round(sum(v["elapsed_s"] for v in ops.values()), 3),
            },
            "operations": ops,
        })
    return {"runs": result, "total_runs": len(result)}


# ── Project Groups (Firestore) ────────────────────────────────────────────────

@router.get("/groups")
def get_groups():
    from agent_service.email_agent.services.firestore_service import list_groups
    return list_groups()


@router.get("/groups/detail")
def get_groups_detail():
    from agent_service.email_agent.services.firestore_service import list_group_details
    groups = list_group_details()
    groups.sort(key=lambda g: g.get("last_activity", ""), reverse=True)
    return groups
