import json
from typing import Any, Dict, List

from openai import OpenAI

SYSTEM_PROMPT = """You are a careful email assistant. Analyse emails and suggest actions.
Never execute — only propose a plan for the user to confirm.
Classify each email as: keep, archive, or label.
Never suggest deleting. If unsure, default to keep.
Respond only in valid JSON:
{"emails":[{"id":"...","subject":"...","suggested_action":"archive","label":"Promotions","reason":"..."}],"summary":"..."}"""

_client = OpenAI()


def classify_emails(emails: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Send emails to GPT-4o-mini and return a structured classification plan."""
    emails_json = json.dumps(emails, ensure_ascii=False)
    response = _client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Classify these emails: {emails_json}"},
        ],
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)
