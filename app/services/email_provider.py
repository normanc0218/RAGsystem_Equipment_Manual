import json
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List


class EmailProvider(ABC):
    """Abstract email provider — swap Gmail for Yahoo by changing EMAIL_PROVIDER env var."""

    @abstractmethod
    def fetch_emails(self, max_results: int = 50) -> List[Dict[str, Any]]:
        """Return list of {id, subject, from, date, snippet}."""

    @abstractmethod
    def archive_email(self, email_id: str) -> bool:
        """Remove email from inbox (no delete)."""

    @abstractmethod
    def label_email(self, email_id: str, label_name: str) -> bool:
        """Apply a label to the email, creating the label if needed."""


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
            raise RuntimeError(
                "Gmail not authenticated. Visit GET /auth/login to authorise."
            )

        with open(self.TOKEN_FILE) as fh:
            data = json.load(fh)

        creds = Credentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.getenv("GMAIL_CLIENT_ID"),
            client_secret=os.getenv("GMAIL_CLIENT_SECRET"),
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
            body={
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        ).execute()
        return new["id"]

    # ── public API ────────────────────────────────────────────────────────────

    def fetch_emails(self, max_results: int = 50) -> List[Dict[str, Any]]:
        service = self._get_service()
        result = service.users().messages().list(
            userId="me", maxResults=max_results, labelIds=["INBOX"]
        ).execute()
        messages = result.get("messages", [])

        emails = []
        for msg in messages:
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
            emails.append(
                {
                    "id": msg["id"],
                    "subject": headers.get("Subject", "(no subject)"),
                    "from": headers.get("From", ""),
                    "date": headers.get("Date", ""),
                    "snippet": detail.get("snippet", ""),
                }
            )
        return emails

    def archive_email(self, email_id: str) -> bool:
        service = self._get_service()
        service.users().messages().modify(
            userId="me",
            id=email_id,
            body={"removeLabelIds": ["INBOX"]},
        ).execute()
        return True

    def label_email(self, email_id: str, label_name: str) -> bool:
        service = self._get_service()
        label_id = self._get_or_create_label(label_name)
        service.users().messages().modify(
            userId="me",
            id=email_id,
            body={"addLabelIds": [label_id]},
        ).execute()
        return True


class YahooProvider(EmailProvider):
    """Stub — replace with IMAP logic when Yahoo support is needed."""

    def fetch_emails(self, max_results: int = 50) -> List[Dict[str, Any]]:
        raise NotImplementedError("Yahoo provider not yet implemented.")

    def archive_email(self, email_id: str) -> bool:
        raise NotImplementedError("Yahoo provider not yet implemented.")

    def label_email(self, email_id: str, label_name: str) -> bool:
        raise NotImplementedError("Yahoo provider not yet implemented.")


def get_email_provider() -> EmailProvider:
    provider = os.getenv("EMAIL_PROVIDER", "gmail").lower()
    if provider == "gmail":
        return GmailProvider()
    if provider == "yahoo":
        return YahooProvider()
    raise ValueError(f"Unknown EMAIL_PROVIDER: {provider!r}")
