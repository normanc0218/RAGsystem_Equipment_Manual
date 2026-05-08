# Email Agent

An AI-powered Gmail inbox organiser built with [Google ADK](https://google.github.io/adk-docs/), OpenAI GPT-4o-mini, and Firestore. Classifies, groups, and archives emails automatically via a Slack bot or interactive CLI.

---

## How It Works

### Organise Workflow

```
User: "organise my emails"
        ↓
  email_agent (router)
        ↓
  1. mailbox_sync_agent     — bootstraps Firestore from existing Gmail labels (skips if already seeded)
        ↓
  2. inbox_processing_agent — fetches → classifies → groups → archives
        ↓
  3. summarization_agent    — generates 2–3 sentence summaries per group
        ↓
  4. audit_agent            — appends to action log
        ↓
  Master agent compiles final report (≤ 500 words)
```

### Email Sorting Algorithm

Emails are classified and grouped in three layers:

**Phase 1 — Domain pre-grouping**
All unprocessed emails are bucketed by sender domain (`@siemens.com`, `@github.com`, etc.) before any LLM call. Each domain bucket is sent to GPT-4o-mini in batches of 20. The prompt enforces a naming format:

```
{Company} {Machine/Product} {Problem/Topic}   (max 6 words)

Examples:
  Siemens S7-1500 Overheating
  ABB Robot Arm Calibration Fault
  Fanuc CNC Spindle Error
  Siemens Billing               ← no machine mentioned
  Promotions / Newsletters      ← special case
```

GPT also flags `should_archive` (resolved, paid, read-only notifications) and `needs_attention` (fault alarms, overdue payments, deadlines).

**Phase 2 — Three-layer vector clustering**

For each classified email, `find_or_create_group()` runs:

| Layer | Condition | Action |
|---|---|---|
| Thread short-circuit | Same Gmail `thread_id` found in Firestore | Merge immediately — thread coherence overrides topic distance |
| Vector similarity ≥ 0.95 | OpenAI `text-embedding-3-small` COSINE similarity | Merge — nearly identical topic |
| Vector similarity 0.70–0.95 | Ambiguous band | Score structural signals: thread match (+1), sender match (+1), name overlap (+1), last activity < 30 days (+1). Score ≥ 2 → merge; else GPT-4o-mini decides |
| Vector similarity < 0.70 | Too different | Create new group |

**Phase 3 — Persist + archive**

- Group metadata (name, embedding, email IDs, senders, thread IDs, summary) → **Firestore**
- Per-email processed flag → **Firestore**
- Archive actions with undo log → **SQLite**
- Session summary (run stats, last sync time) → **ADK session state**

---

## Project Structure

```
.
├── agent_service/              # ADK agent runner
│   ├── main.py                 # Session service, runner, interactive CLI entry point
│   └── email_agent/            # ADK agent package
│       ├── email_agent.py      # Master router agent
│       ├── database.py         # SQLite setup (SQLAlchemy)
│       ├── models/
│       │   └── action_log.py   # Archive/undo action log model
│       ├── services/
│       │   ├── email_provider.py     # Gmail API wrapper
│       │   ├── embedding_service.py  # OpenAI text-embedding-3-small
│       │   ├── firestore_service.py  # Firestore read/write + vector search
│       │   └── grouping_service.py   # Three-layer clustering algorithm
│       ├── sub_agents/
│       │   ├── mailbox_sync_agent.py
│       │   ├── inbox_processing_agent.py
│       │   ├── summarization_agent.py
│       │   ├── digest_agent.py
│       │   ├── audit_agent.py
│       │   └── casual_agent.py
│       └── tools/
│           ├── email_tools.py   # sync_gmail_labels, batch_process_emails, archive_email
│           ├── log_tools.py     # get_action_log, undo_action, undo_last_action, preview_undo
│           ├── project_tools.py # group_emails, summarize_group
│           └── digest_tools.py  # daily_digest
│
├── app/                        # FastAPI server (Slack bot + REST)
│   ├── server.py               # FastAPI app entry point
│   ├── util.py                 # Health, OAuth, logs, groups routes
│   └── routers/
│       └── slack.py            # Slack slash commands (/organize, /digest, /undo)
│
├── reset.py                    # Clears Firestore, SQLite, and ADK session DB
├── .env                        # Environment variables (see below)
└── requirements_email_agent.txt
```

---

## Storage Layers

| Layer | Technology | Stores |
|---|---|---|
| **Email groups** | Firestore `email_groups` | Group name, embedding, email IDs, senders, thread IDs, summary |
| **Processed emails** | Firestore `email_summaries` | Per-email `processed=True` flag, subject, sender, snippet |
| **Action log** | SQLite `email_agent.db` | Every archive/undo with `log_id`, status, `undone_at` — supports undo |
| **Session state** | ADK SQLite `email_agent_sessions.db` | `user_id`, `user_name`, `dry_run`, `interaction_history`, `last_sync_time`, `emails_processed_total` |

### Session State Schema

```python
{
    "user_id": str,               # Slack user ID or "local_user"
    "user_name": str,             # Display name (optional, set at session creation)
    "dry_run": bool,              # When True, skips all Gmail write calls
    "interaction_history": [      # High-level summary per ORGANIZE run
        {
            "action": "organize",
            "fetched": int,
            "processed": int,
            "groups": int,
            "archived": int,
            "timestamp": str,     # ISO 8601
        }
    ],
    "last_sync_time": str | None, # ISO timestamp of last Gmail label sync
    "emails_processed_total": int # Cumulative count across all sessions
}
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements_email_agent.txt
```

### 2. Configure environment variables

Copy `.env.example` to `.env` and fill in:

```env
# OpenAI
OPENAI_API_KEY=sk-...

# Google Cloud / Firestore
GOOGLE_APPLICATION_CREDENTIALS=firestore-key.json
GOOGLE_CLOUD_PROJECT=your-project-id

# Gmail OAuth
GMAIL_CLIENT_ID=...
GMAIL_CLIENT_SECRET=...
GMAIL_REDIRECT_URI=http://localhost:8000/auth/callback

# Slack (for bot mode)
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...

# Agent settings
DRY_RUN=true          # Set to false to write changes to Gmail
EMAIL_PROVIDER=gmail
ADK_USER_ID=local_user
```

### 3. Authenticate Gmail

Start the FastAPI server and visit `/auth/login`:

```bash
uvicorn app.server:api --reload --port 8000
# then open http://localhost:8000/auth/login
```

This saves `gmail_token.json` to the project root.

---

## Running

### Interactive CLI (ADK runner)

```bash
python -m agent_service.main
```

Starts an interactive session. Type any message:

```
You: organise my emails
You: show me a digest
You: undo the last action
You: exit
```

### ADK Web UI

```bash
adk web agent_service --allow_origins "*"
```

### Slack Bot + REST API

```bash
uvicorn app.server:api --reload --port 8000
```

Exposes:
- `POST /slack/events` — Slack event webhook
- `GET  /health`
- `GET  /auth/login` → `GET /auth/callback`
- `GET  /logs?limit=100`
- `GET  /groups`
- `GET  /groups/detail`

### Slack slash commands

| Command | Action |
|---|---|
| `/organize` | Run the full inbox organise workflow |
| `/digest` | Generate a daily digest of current groups |
| `/undo <id>` | Reverse a specific action by log ID |

### Reset state for a fresh run

```bash
python reset.py
```

Clears Firestore groups, SQLite action logs, and the ADK session database.

---

## Undo Safety

The audit agent uses three safe entry points — the LLM never guesses a `log_id`:

| User says | Behaviour |
|---|---|
| "undo the last action" | Calls `undo_last_action()` — picks the most recent undoable entry automatically |
| "undo the invoice email" | Calls `preview_undo(description)` — returns candidates, asks user to confirm `log_id` before acting |
| `/undo 42` | Calls `undo_action(log_id=42)` directly |

Only actions with `status=success` can be undone. Dry-run actions are not reversible (nothing was written to Gmail).
