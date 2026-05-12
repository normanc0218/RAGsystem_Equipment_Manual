"""
Label setup service.

Detects Gmail labels that have no Firestore group (empty labels the user created
manually) and returns the data needed to prompt the user in Slack.

Once the user provides a description, seed_group_from_description() embeds it
and saves a Firestore group so future emails can be matched against it.
"""
import json
import logging

from openai import OpenAI

from .embedding_service import get_embedding
from .firestore_service import save_group, list_groups

logger = logging.getLogger(__name__)


def find_empty_user_labels() -> list[dict]:
    """Return Gmail user labels that have no emails processed into them yet.

    Covers two cases:
    - Label exists in Gmail but has no Firestore group at all
    - Label has a Firestore group but email_count = 0 (seeded from description only)

    Returns list of {"id": ..., "name": ...}
    """
    from .email_provider import GmailProvider

    provider = GmailProvider()
    gmail_labels = provider.list_user_labels()

    groups_by_name = {g["name"].lower(): g for g in list_groups()}
    return [
        lbl for lbl in gmail_labels
        if (
            lbl["name"].lower() not in groups_by_name  # no Firestore group at all
            or (
                groups_by_name[lbl["name"].lower()].get("email_count", 0) == 0
                and not groups_by_name[lbl["name"].lower()].get("description", "").strip()
            )  # group exists but no emails and no description
        )
    ]


def generate_label_options(label_name: str) -> list[str]:
    """Ask GPT to suggest 3 short descriptions for what belongs in this label."""
    client = OpenAI()
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "Generate exactly 3 short options (max 6 words each) describing "
                    "what kind of emails belong in a Gmail label with the given name. "
                    'Return JSON: {"options": ["...", "...", "..."]}'
                ),
            },
            {"role": "user", "content": f"Label name: {label_name}"},
        ],
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    data = json.loads(resp.choices[0].message.content)
    return data.get("options", [])[:3]


def seed_group_from_description(label_name: str, description: str) -> dict:
    """Embed the user's description and save a Firestore group for this label.

    The group starts with no emails — the embedding alone is enough for
    future incoming emails to be matched against it via vector similarity.
    """
    full_text = f"{label_name} {description}"
    embedding = get_embedding(full_text)

    group_id = save_group({
        "name": label_name,
        "description": description,
        "summary": "",
        "embedding": embedding,
        "email_ids": [],
        "senders": [],
        "thread_ids": [],
        "email_count": 0,
        "source": "user",
    })

    logger.info("Seeded Firestore group '%s' from user description.", label_name)
    return {"group_id": group_id, "name": label_name}
