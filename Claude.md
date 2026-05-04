# Email Agent MVP

## What we're building
A Slack bot that reads Gmail, uses GPT-4o-mini to classify emails, shows a plan in Slack, waits for user confirmation, then archives/labels. Also sends a daily digest at 8am.

## Stack
- **Backend**: FastAPI (existing)
- **Email**: Gmail API (`google-auth`, `googleapiclient`) — abstracted so Yahoo IMAP can be swapped in later
- **AI**: OpenAI `gpt-4o-mini`
- **Slack**: Slack Bolt for Python (`slack-bolt`)
- **DB**: SQLite via SQLAlchemy (action logs only)

## File structure
```
app/
  routers/slack.py          # Slack webhook + button handlers
  services/
    email_provider.py       # Abstract base + GmailProvider + YahooProvider stub
    ai_service.py           # GPT-4o-mini calls, returns structured JSON plan
    scheduler.py            # Daily digest job
  models/action_log.py      # timestamp, user, action, email_id, status
.env.example
```

## AI prompt (system)
```
You are a careful email assistant. Analyse emails and suggest actions.
Never execute — only propose a plan for the user to confirm.
Classify each email as: keep, archive, or label.
Never suggest deleting. If unsure, default to keep.
Respond only in valid JSON:
{"emails":[{"id":"...","subject":"...","suggested_action":"archive","label":"Promotions","reason":"..."}],"summary":"..."}
```

## AI call
```python
from openai import OpenAI
client = OpenAI()

response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Classify these emails: {emails_json}"}
    ]
)
```

## Slack commands
- `/organise` → fetch 50 emails → classify → post Block Kit plan with Confirm/Cancel buttons
- `/digest` → manually trigger daily digest (for testing)

## Safety rules (non-negotiable)
1. No write operation without a prior Confirm button click — enforce in code, not just prompt
2. Never delete — only archive
3. Max 50 emails per confirmed action
4. `DRY_RUN=true` env var skips all Gmail write calls
5. Log every executed action to SQLite

## Env vars
```
OPENAI_API_KEY=
GMAIL_CLIENT_ID=
GMAIL_CLIENT_SECRET=
SLACK_BOT_TOKEN=
SLACK_SIGNING_SECRET=
SLACK_DIGEST_CHANNEL=#general
DRY_RUN=true
DIGEST_TIMEZONE=America/Toronto
```

## Gmail OAuth
- `GET /auth/login` → Google consent
- `GET /auth/callback` → save tokens to `gmail_token.json`
- Auto-refresh on startup

## Done when
- [ ] `/organise` shows classified plan in Slack
- [ ] Confirm executes, Cancel does nothing
- [ ] Daily digest posts at 8am
- [ ] All actions logged at `GET /logs`
- [ ] `DRY_RUN=true` works safely
- [ ] `YahooProvider` stub exists for future swap