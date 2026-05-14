import base64
import json
import logging
import os
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Gmail-style category label → group name (shared by GmailProvider and FakeProvider)
GMAIL_LABEL_GROUPS: dict[str, str] = {
    "CATEGORY_PROMOTIONS": "Promotions",
    "CATEGORY_SOCIAL":     "Social Notifications",
    "CATEGORY_UPDATES":    "Automated Updates",
    "CATEGORY_FORUMS":     "Mailing Lists",
}

# Standard RFC headers that signal newsletter / mailing-list / bulk mail
_TICKET_RE = re.compile(
    r'\[([A-Z]+-\d+)\]'                          # [JIRA-123]
    r'|(?:Invoice|PO|Order|Case)\s*#\s*\d+'       # Invoice #4521
    r'|^\[([^\]]{2,30})\]',                        # [PROJECT] prefix
    re.IGNORECASE,
)

# Emails that are safe to archive automatically in Layer 0
_ARCHIVE_FAST_GROUPS = {"Promotions", "Social Notifications", "Automated Updates", "Mailing Lists", "Newsletters"}


class EmailProvider(ABC):
    """Abstract email provider — swap Gmail for Yahoo by changing EMAIL_PROVIDER env var."""

    @abstractmethod
    def fetch_emails(self, max_results: int = 200, random_sample: bool = False) -> List[Dict[str, Any]]:
        """Return list of {id, thread_id, subject, from, date, snippet}."""

    @abstractmethod
    def fetch_emails_page(self, page_size: int = 50, page_token: str | None = None) -> tuple[List[Dict[str, Any]], str | None]:
        """Fetch one page of inbox emails. Returns (emails, next_page_token)."""

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

    @abstractmethod
    def list_user_labels(self) -> list[dict]:
        """Return all user-created labels as list of {id, name}."""

    @abstractmethod
    def fetch_emails_by_label(self, label_id: str, max_results: int = 100) -> list[dict]:
        """Return emails carrying a specific label."""

    def get_fast_group(self, email: dict) -> str | None:
        """Return a group name from deterministic signals — no LLM needed.

        Uses universal RFC headers available from any provider.
        Returns None if the email needs LLM classification.
        """
        # List-ID → mailing list (RFC 2919, present on all list mail)
        list_id = email.get("list_id", "")
        if list_id:
            name = list_id.strip("<>").split(".")[0].replace("-", " ").title()
            return f"Mailing List: {name}" if name else "Mailing Lists"

        # Precedence: bulk / list → newsletter or automated mail
        prec = (email.get("precedence") or "").lower()
        if prec in ("bulk", "list"):
            return "Newsletters"

        # List-Unsubscribe header → newsletter even without Precedence
        if email.get("list_unsubscribe"):
            return "Newsletters"

        # Subject ticket/order pattern → support or billing group
        subject = email.get("subject", "")
        m = _TICKET_RE.search(subject)
        if m:
            ticket_id = m.group(1) or ""
            if ticket_id:
                project = ticket_id.split("-")[0]
                return f"{project} Tickets"
            return "Support Tickets"

        return None


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
            # Fall back to env vars, then to values already stored in the token file
            client_id = os.getenv("GMAIL_CLIENT_ID") or data.get("client_id")
            client_secret = os.getenv("GMAIL_CLIENT_SECRET") or data.get("client_secret")

        creds = Credentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=self.SCOPES,
        )

        # Refresh proactively if expired, then persist the new token to disk
        # so the next startup doesn't hit a 401.
        if creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            with open(self.TOKEN_FILE, "w") as fh:
                json.dump({
                    "token": creds.token,
                    "refresh_token": creds.refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                }, fh)

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

    def _batch_fetch_metadata(self, message_stubs: list[dict]) -> list[dict]:
        """Fetch metadata for a list of {id} stubs in one HTTP batch request.

        Gmail batch endpoint packs up to 100 sub-requests into a single
        multipart/mixed HTTP call, so N emails cost 1 round-trip instead of N.
        Scales automatically — just pass any number of stubs; this method
        chunks into ≤100-item batches as needed.
        """
        service = self._get_service()
        results: dict[str, dict] = {}

        def _callback(request_id: str, response, exception):
            if exception:
                logger.warning("Batch metadata failed for %s: %s", request_id, exception)
                return
            headers = {
                h["name"].lower(): h["value"]
                for h in response.get("payload", {}).get("headers", [])
            }
            results[request_id] = {
                "id": request_id,
                "thread_id": response.get("threadId", ""),
                "subject": headers.get("subject", "(no subject)"),
                "from": headers.get("from", ""),
                "date": headers.get("date", ""),
                "snippet": response.get("snippet", ""),
                # Layer 0 grouping signals
                "label_ids": response.get("labelIds", []),
                "list_id": headers.get("list-id", ""),
                "precedence": headers.get("precedence", ""),
                "list_unsubscribe": headers.get("list-unsubscribe", ""),
            }

        _METADATA_HEADERS = ["Subject", "From", "Date", "List-ID", "Precedence", "List-Unsubscribe"]

        # Gmail batch limit is 100 sub-requests per call
        for i in range(0, len(message_stubs), 100):
            chunk = message_stubs[i: i + 100]
            batch = service.new_batch_http_request(callback=_callback)
            for stub in chunk:
                batch.add(
                    service.users().messages().get(
                        userId="me", id=stub["id"],
                        format="metadata",
                        metadataHeaders=_METADATA_HEADERS,
                    ),
                    request_id=stub["id"],
                )
            batch.execute()

        # Preserve original order; skip any stubs that errored
        return [results[s["id"]] for s in message_stubs if s["id"] in results]

    def fetch_emails(self, max_results: int = 200, random_sample: bool = False) -> List[Dict[str, Any]]:
        import random
        service = self._get_service()

        pool_size = min(500, max_results * 3) if random_sample else max_results

        message_stubs = []
        page_token = None
        while len(message_stubs) < pool_size:
            kwargs = dict(
                userId="me",
                maxResults=min(100, pool_size - len(message_stubs)),
                q="category:primary",
            )
            if page_token:
                kwargs["pageToken"] = page_token
            result = service.users().messages().list(**kwargs).execute()
            message_stubs.extend(result.get("messages", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break

        if random_sample and len(message_stubs) > max_results:
            message_stubs = random.sample(message_stubs, max_results)

        return self._batch_fetch_metadata(message_stubs)

    def fetch_emails_page(self, page_size: int = 50, page_token: str | None = None) -> tuple[List[Dict[str, Any]], str | None]:
        service = self._get_service()
        kwargs = dict(userId="me", maxResults=min(page_size, 100), q="category:primary")
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        next_token = result.get("nextPageToken")
        message_stubs = result.get("messages", [])
        return self._batch_fetch_metadata(message_stubs), next_token

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

    def list_user_labels(self) -> list[dict]:
        service = self._get_service()
        all_labels = service.users().labels().list(userId="me").execute().get("labels", [])
        system_prefixes = ("INBOX", "SENT", "DRAFT", "SPAM", "TRASH", "UNREAD",
                           "STARRED", "IMPORTANT", "CATEGORY_", "CHAT")
        return [
            {"id": lbl["id"], "name": lbl["name"]}
            for lbl in all_labels
            if not any(lbl["id"].startswith(p) for p in system_prefixes)
        ]

    def get_fast_group(self, email: dict) -> str | None:
        for label in email.get("label_ids", []):
            if label in GMAIL_LABEL_GROUPS:
                return GMAIL_LABEL_GROUPS[label]
        return super().get_fast_group(email)

    def fetch_emails_by_label(self, label_id: str, max_results: int = 100) -> list[dict]:
        service = self._get_service()
        message_ids = []
        page_token = None
        while len(message_ids) < max_results:
            kwargs = dict(userId="me", labelIds=[label_id],
                          maxResults=min(100, max_results - len(message_ids)))
            if page_token:
                kwargs["pageToken"] = page_token
            result = service.users().messages().list(**kwargs).execute()
            message_ids.extend(result.get("messages", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break

        emails = []
        for msg in message_ids:
            detail = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
            emails.append({
                "id": msg["id"],
                "thread_id": detail.get("threadId", ""),
                "subject": headers.get("Subject", "(no subject)"),
                "from": headers.get("From", ""),
                "date": headers.get("Date", ""),
                "snippet": detail.get("snippet", ""),
            })
        return emails


class FakeProvider(EmailProvider):
    """Returns hardcoded sample emails for local testing without Gmail credentials."""

    _EMAILS = [
        {"id": "fake001", "thread_id": "thread-promo-1",      "subject": "50% off your next order!",            "from": "deals@shop.com",                  "date": "Mon, 4 May 2026 08:00:00 +0000", "snippet": "Limited time offer just for you.",              "label_ids": ["CATEGORY_PROMOTIONS"], "list_id": "", "precedence": "",     "list_unsubscribe": ""},
        {"id": "fake002", "thread_id": "thread-billing-1",    "subject": "Your invoice #4821 is ready",         "from": "billing@saas.io",                 "date": "Mon, 4 May 2026 09:15:00 +0000", "snippet": "Please review and pay by May 15.",             "label_ids": ["CATEGORY_UPDATES"],    "list_id": "", "precedence": "",     "list_unsubscribe": ""},
        {"id": "fake003", "thread_id": "thread-newsletter-1", "subject": "Weekly newsletter: AI trends",        "from": "editor@aiweekly.com",             "date": "Sun, 3 May 2026 18:00:00 +0000", "snippet": "Top 5 AI stories this week.",                  "label_ids": [],                      "list_id": "<weekly.aiweekly.com>", "precedence": "list", "list_unsubscribe": "<mailto:unsub@aiweekly.com>"},
        {"id": "fake004", "thread_id": "thread-work-1",       "subject": "Meeting notes from Monday standup",  "from": "team@company.com",                "date": "Mon, 4 May 2026 10:30:00 +0000", "snippet": "Action items assigned to you.",               "label_ids": [],                      "list_id": "", "precedence": "",     "list_unsubscribe": ""},
        {"id": "fake005", "thread_id": "thread-work-2",       "subject": "Your GitHub PR was merged",          "from": "noreply@github.com",              "date": "Mon, 4 May 2026 11:00:00 +0000", "snippet": "Pull request #42 was merged into main.",      "label_ids": ["CATEGORY_UPDATES"],    "list_id": "", "precedence": "",     "list_unsubscribe": ""},
        {"id": "fake006", "thread_id": "thread-promo-2",      "subject": "Flash sale ends tonight!",           "from": "promo@retailer.com",              "date": "Mon, 4 May 2026 07:00:00 +0000", "snippet": "Don't miss out — 24 hours only.",             "label_ids": ["CATEGORY_PROMOTIONS"], "list_id": "", "precedence": "bulk", "list_unsubscribe": ""},
        {"id": "fake007", "thread_id": "thread-security-1",   "subject": "Security alert: new login detected", "from": "security@accounts.google.com",    "date": "Mon, 4 May 2026 06:45:00 +0000", "snippet": "New sign-in from Chrome on Mac.",             "label_ids": ["CATEGORY_UPDATES"],    "list_id": "", "precedence": "",     "list_unsubscribe": ""},
        {"id": "fake008", "thread_id": "thread-work-3",       "subject": "Q2 OKR review — please fill in",    "from": "manager@company.com",             "date": "Fri, 1 May 2026 16:00:00 +0000", "snippet": "Please update your OKR tracker by EOD Friday.", "label_ids": [],                      "list_id": "", "precedence": "",     "list_unsubscribe": ""},
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

    def get_fast_group(self, email: dict) -> str | None:
        for label in email.get("label_ids", []):
            if label in GMAIL_LABEL_GROUPS:
                return GMAIL_LABEL_GROUPS[label]
        return super().get_fast_group(email)

    def fetch_emails(self, max_results: int = 200, random_sample: bool = False) -> List[Dict[str, Any]]:
        return self._EMAILS[:max_results]

    def fetch_emails_page(self, page_size: int = 50, page_token: str | None = None) -> tuple[List[Dict[str, Any]], str | None]:
        return self._EMAILS[:page_size], None

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

    def list_user_labels(self) -> list[dict]:
        return [{"id": "fake-label-work", "name": "Work"}, {"id": "fake-label-billing", "name": "Billing"}]

    def fetch_emails_by_label(self, label_id: str, max_results: int = 100) -> list[dict]:
        mapping = {
            "fake-label-work": ["fake004", "fake005", "fake008"],
            "fake-label-billing": ["fake002"],
        }
        ids = mapping.get(label_id, [])
        return [e for e in self._EMAILS if e["id"] in ids][:max_results]


class YahooProvider(EmailProvider):
    """Stub — replace with IMAP logic when Yahoo support is needed."""

    def fetch_emails(self, max_results: int = 200, random_sample: bool = False) -> List[Dict[str, Any]]:
        raise NotImplementedError("Yahoo provider not yet implemented.")

    def fetch_emails_page(self, page_size: int = 50, page_token: str | None = None) -> tuple[List[Dict[str, Any]], str | None]:
        raise NotImplementedError

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

    def list_user_labels(self) -> list[dict]:
        raise NotImplementedError

    def fetch_emails_by_label(self, label_id: str, max_results: int = 100) -> list[dict]:
        raise NotImplementedError


_provider_cache: dict[str, EmailProvider] = {}


def get_email_provider() -> EmailProvider:
    provider = os.getenv("EMAIL_PROVIDER", "gmail").lower()
    if provider not in _provider_cache:
        if provider == "gmail":
            _provider_cache[provider] = GmailProvider()
        elif provider == "yahoo":
            _provider_cache[provider] = YahooProvider()
        elif provider == "fake":
            _provider_cache[provider] = FakeProvider()
        else:
            raise ValueError(f"Unknown EMAIL_PROVIDER: {provider!r}")
    return _provider_cache[provider]
