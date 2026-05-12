"""
POST /gmail/push — receives Gmail push notifications delivered via Google Pub/Sub.

Pub/Sub wraps the Gmail notification in a base64-encoded message body:
    {
        "message": {
            "data": "<base64-encoded JSON>",
            "messageId": "...",
            "publishTime": "..."
        },
        "subscription": "projects/.../subscriptions/..."
    }

The decoded data is: {"emailAddress": "user@gmail.com", "historyId": "12345"}

Must always return 2xx — a non-2xx response tells Pub/Sub to retry delivery.
"""
import base64
import json
import logging

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/gmail/push")
async def gmail_push(request: Request):
    try:
        body = await request.json()
        data_b64 = body.get("message", {}).get("data", "")
        if not data_b64:
            return {"status": "ok", "detail": "no data field"}

        payload = json.loads(base64.b64decode(data_b64).decode("utf-8"))

        from agent_service.email_agent.services.gmail_watch_service import process_push_notification
        result = process_push_notification(payload)
        logger.info("Gmail push processed: %s", result)
        return {"status": "ok", **result}

    except Exception as exc:
        # Log but always return 200 so Pub/Sub does not keep retrying
        logger.exception("Error handling Gmail push notification")
        return {"status": "error", "detail": str(exc)}
