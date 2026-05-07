import base64
import json
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class EmailProvider(ABC):
    """Abstract email provider — swap Gmail for Yahoo by changing EMAIL_PROVIDER env var."""

    @abstractmethod
    def fetch_emails(self, max_results: int = 200, random_sample: bool = False) -> List[Dict[str, Any]]:
        """Return list of {id, thread_id, subject, from, date, snippet}."""

    @abstractmethod
    def get_email_body(self, email_id: str) -> str:
        """Return the full plain-text body of an email."""

    @abstractmethod
    def archive_email(self, email_id: str) -> bool:
        """Remove email from inbox (no delete)."""

    @abstractmethod
    def unarchive_email(self, email_id: str) -> bool:
        """Restore email to inbox (undo archive)."""

    @abstractmethod
    def label_email(self, email_id: str, label_name: str) -> bool:
        """Apply a label to the email, creating the label if needed."""

    @abstractmethod
    def remove_label(self, email_id: str, label_name: str) -> bool:
        """Remove a label from an email (undo label)."""


class GmailProvider(EmailProvider):
    TOKEN_FILE = "gmail_token.json"
    SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

    def __init__(self) -> None:
        self._service = None

    # ── internal ──────────────────────────────────────────────────────────────

    def _get_service(self):
        if self._service:
            return self._service

        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        if not os.path.exists(self.TOKEN_FILE):
            raise RuntimeError("Gmail not authenticated. Visit GET /auth/login to authorise.")

        with open(self.TOKEN_FILE) as fh:
            data = json.load(fh)

        secrets_file = os.getenv("GMAIL_CLIENT_SECRETS_FILE")
        if secrets_file and os.path.exists(secrets_file):
            with open(secrets_file) as f:
                secrets = json.load(f)
            web = secrets.get("web", {})
            client_id = web.get("client_id")
            client_secret = web.get("client_secret")
        else:
            client_id = os.getenv("GMAIL_CLIENT_ID")
            client_secret = os.getenv("GMAIL_CLIENT_SECRET")

        creds = Credentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=self.SCOPES,
        )
        self._service = build("gmail", "v1", credentials=creds)
        return self._service

    def _get_or_create_label(self, label_name: str) -> str:
        service = self._get_service()
        existing = service.users().labels().list(userId="me").execute().get("labels", [])
        for lbl in existing:
            if lbl["name"].lower() == label_name.lower():
                return lbl["id"]
        new = service.users().labels().create(
            userId="me",
            body={"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
        ).execute()
        return new["id"]

    def _find_label_id(self, label_name: str) -> Optional[str]:
        service = self._get_service()
        labels = service.users().labels().list(userId="me").execute().get("labels", [])
        for lbl in labels:
            if lbl["name"].lower() == label_name.lower():
                return lbl["id"]
        return None

    def _extract_body(self, payload: dict) -> str:
        mime = payload.get("mimeType", "")
        if mime == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        if "parts" in payload:
            for part in payload["parts"]:
                result = self._extract_body(part)
                if result:
                    return result
        return ""

    # ── public API ────────────────────────────────────────────────────────────

    def fetch_emails(self, max_results: int = 200, random_sample: bool = False) -> List[Dict[str, Any]]:
        import random
        service = self._get_service()

        pool_size = min(500, max_results * 3) if random_sample else max_results

        message_ids = []
        page_token = None
        while len(message_ids) < pool_size:
            kwargs = dict(
                userId="me",
                maxResults=min(100, pool_size - len(message_ids)),
                q="-in:spam -in:trash -category:promotions",
            )
            if page_token:
                kwargs["pageToken"] = page_token
            result = service.users().messages().list(**kwargs).execute()
            message_ids.extend(result.get("messages", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break

        if random_sample and len(message_ids) > max_results:
            message_ids = random.sample(message_ids, max_results)

        emails = []
        for msg in message_ids:
            detail = service.users().messages().get(
                userId="me",
                id=msg["id"],
                format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            ).execute()
            headers = {
                h["name"]: h["value"]
                for h in detail.get("payload", {}).get("headers", [])
            }
            emails.append({
                "id": msg["id"],
                "thread_id": detail.get("threadId", ""),
                "subject": headers.get("Subject", "(no subject)"),
                "from": headers.get("From", ""),
                "date": headers.get("Date", ""),
                "snippet": detail.get("snippet", ""),
            })
        return emails

    def get_email_body(self, email_id: str) -> str:
        service = self._get_service()
        msg = service.users().messages().get(userId="me", id=email_id, format="full").execute()
        body = self._extract_body(msg.get("payload", {}))
        return body[:4000] if body else msg.get("snippet", "(no body)")

    def archive_email(self, email_id: str) -> bool:
        service = self._get_service()
        service.users().messages().modify(
            userId="me", id=email_id, body={"removeLabelIds": ["INBOX"]}
        ).execute()
        return True

    def unarchive_email(self, email_id: str) -> bool:
        service = self._get_service()
        service.users().messages().modify(
            userId="me", id=email_id, body={"addLabelIds": ["INBOX"]}
        ).execute()
        return True

    def label_email(self, email_id: str, label_name: str) -> bool:
        service = self._get_service()
        label_id = self._get_or_create_label(label_name)
        service.users().messages().modify(
            userId="me", id=email_id, body={"addLabelIds": [label_id]}
        ).execute()
        return True

    def remove_label(self, email_id: str, label_name: str) -> bool:
        service = self._get_service()
        label_id = self._find_label_id(label_name)
        if label_id:
            service.users().messages().modify(
                userId="me", id=email_id, body={"removeLabelIds": [label_id]}
            ).execute()
        return True


class FakeProvider(EmailProvider):
    """Returns hardcoded sample emails for local testing without Gmail credentials."""

    _EMAILS = [
        {"id": "fake001", "thread_id": "thread-promo-1", "subject": "50% off your next order!", "from": "deals@shop.com", "date": "Mon, 4 May 2026 08:00:00 +0000", "snippet": "Limited time offer just for you."},
        {"id": "fake002", "thread_id": "thread-billing-1", "subject": "Your invoice #4821 is ready", "from": "billing@saas.io", "date": "Mon, 4 May 2026 09:15:00 +0000", "snippet": "Please review and pay by May 15."},
        {"id": "fake003", "thread_id": "thread-newsletter-1", "subject": "Weekly newsletter: AI trends", "from": "editor@aiweekly.com", "date": "Sun, 3 May 2026 18:00:00 +0000", "snippet": "Top 5 AI stories this week."},
        {"id": "fake004", "thread_id": "thread-work-1", "subject": "Meeting notes from Monday standup", "from": "team@company.com", "date": "Mon, 4 May 2026 10:30:00 +0000", "snippet": "Action items assigned to you."},
        {"id": "fake005", "thread_id": "thread-work-2", "subject": "Your GitHub PR was merged", "from": "noreply@github.com", "date": "Mon, 4 May 2026 11:00:00 +0000", "snippet": "Pull request #42 was merged into main."},
        {"id": "fake006", "thread_id": "thread-promo-2", "subject": "Flash sale ends tonight!", "from": "promo@retailer.com", "date": "Mon, 4 May 2026 07:00:00 +0000", "snippet": "Don't miss out — 24 hours only."},
        {"id": "fake007", "thread_id": "thread-security-1", "subject": "Security alert: new login detected", "from": "security@accounts.google.com", "date": "Mon, 4 May 2026 06:45:00 +0000", "snippet": "New sign-in from Chrome on Mac."},
        {"id": "fake008", "thread_id": "thread-work-3", "subject": "Q2 OKR review — please fill in", "from": "manager@company.com", "date": "Fri, 1 May 2026 16:00:00 +0000", "snippet": "Please update your OKR tracker by EOD Friday."},
    ]

    _BODIES = {
        "fake001": "Hi there! Use code SAVE50 at checkout for 50% off your next order. Offer expires midnight.",
        "fake002": "Invoice #4821 for $299.00 is now available. Due date: May 15 2026. Click to view and pay.",
        "fake003": "This week in AI: GPT-5 rumours, Google Gemini updates, open-source LLM roundup, and more.",
        "fake004": "Monday standup notes:\n- Alice: finished auth module\n- Bob: working on dashboard\n- You: review PR #42 by Wednesday",
        "fake005": "Pull request #42 'Add dark mode support' has been merged into main by alice. View the diff on GitHub.",
        "fake006": "Flash sale! 70% off electronics. Ends tonight at midnight. Shop now before it's too late.",
        "fake007": "We noticed a new sign-in to your Google Account from Chrome on Mac in Toronto, CA. If this was you, no action needed.",
        "fake008": "Hi team, Q2 OKR review is due. Please update your tracker with progress on key results by EOD Friday.",
    }

    def fetch_emails(self, max_results: int = 200, random_sample: bool = False) -> List[Dict[str, Any]]:
        return self._EMAILS[:max_results]

    def get_email_body(self, email_id: str) -> str:
        return self._BODIES.get(email_id, "(no body available)")

    def archive_email(self, email_id: str) -> bool:
        return True

    def unarchive_email(self, email_id: str) -> bool:
        return True

    def label_email(self, email_id: str, label_name: str) -> bool:
        return True

    def remove_label(self, email_id: str, label_name: str) -> bool:
        return True


class YahooProvider(EmailProvider):
    """Stub — replace with IMAP logic when Yahoo support is needed."""

    def fetch_emails(self, max_results: int = 200, random_sample: bool = False) -> List[Dict[str, Any]]:
        raise NotImplementedError("Yahoo provider not yet implemented.")

    def get_email_body(self, email_id: str) -> str:
        raise NotImplementedError

    def archive_email(self, email_id: str) -> bool:
        raise NotImplementedError

    def unarchive_email(self, email_id: str) -> bool:
        raise NotImplementedError

    def label_email(self, email_id: str, label_name: str) -> bool:
        raise NotImplementedError

    def remove_label(self, email_id: str, label_name: str) -> bool:
        raise NotImplementedError


def get_email_provider() -> EmailProvider:
    provider = os.getenv("EMAIL_PROVIDER", "gmail").lower()
    if provider == "gmail":
        return GmailProvider()
    if provider == "yahoo":
        return YahooProvider()
    if provider == "fake":
        return FakeProvider()
    raise ValueError(f"Unknown EMAIL_PROVIDER: {provider!r}")
