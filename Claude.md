# Email Agent

## What we're building
A Slack bot that autonomously organizes Gmail using Google ADK + GPT-4o-mini. It learns from the user's existing Gmail organization, then extends that structure to new unorganized emails using vector similarity.

## Stack
- **Agent**: Google ADK (`google-adk`) with LiteLLM routing to `openai/gpt-4o-mini`
- **Backend**: FastAPI (`app_main.py`) — OAuth, logs API, groups API
- **Email**: Gmail API abstracted behind `EmailProvider` — swap to Yahoo IMAP via `EMAIL_PROVIDER` env var
- **Vector DB**: Firestore with KNN vector index (COSINE, 1536 dims via `text-embedding-3-small`)
- **AI**: OpenAI `gpt-4o-mini` (agent LLM + group summaries) + `text-embedding-3-small` (embeddings)
- **Slack**: Slack Bolt — `/organize`, `/digest`, `/undo` commands
- **Databases**:
  - `email_agent.db` (SQLite) — action logs only (archive/label/undo history)
  - Firestore `email_groups` — groups with embeddings, summaries, email IDs
  - Firestore `email_summaries` — per-email processed flag + group assignment

## Agent workflow (two phases)

### Phase 1 — Bootstrap from existing Gmail organization
When a user first installs the app (or runs `/organize` for the first time):
1. Call `sync_gmail_labels` to read all user-created Gmail labels
2. For each label, create a Firestore group with the label name
3. Fetch emails under that label and add them to the group
4. Generate an embedding for each group — this seeds the vector DB with the user's existing mental model
5. Mark all those emails as processed in `email_summaries`

**Key insight:** the vector DB now reflects *the user's* organization style, not the agent's guesses.

### Phase 2 — Process unorganized emails
After Phase 1 (or on subsequent runs):
1. Call `get_emails` — automatically skips already-processed emails
2. For each unorganized email, run three-layer clustering:
   - **Layer 1** — vector similarity against existing groups (Firestore KNN, COSINE). If similarity > 0.95 → merge. If < 0.70 → new group.
   - **Layer 2** — structural signals (same thread_id, same sender, name overlap, recency < 30 days). Score ≥ 2 → merge.
   - **Layer 3** — AI fallback (gpt-4o-mini) for ambiguous 0.70–0.95 band: "join or new?"
3. Archive promotional/newsletter emails
4. Call `summarize_group` for each group touched
5. Call `get_action_log` and return a markdown report

## File structure
```
app_main.py                         # FastAPI entry point (OAuth, /logs, /groups)
test_agent.py                       # Terminal test runner (no Slack needed)
email_agent/
  __init__.py                       # Exports root_agent
  agent.py                          # ADK Agent definition + INSTRUCTION
  tools/
    email_tools.py                  # get_emails, archive_email, sync_gmail_labels
    project_tools.py                # group_emails, summarize_group
    log_tools.py                    # get_action_log, undo_action
app/
  database.py                       # SQLite session + Base
  models/action_log.py              # ActionLog ORM model
  routers/slack.py                  # Slack Bolt + /organize /digest /undo handlers
  services/
    email_provider.py               # EmailProvider ABC + GmailProvider + FakeProvider + YahooStub
    embedding_service.py            # OpenAI text-embedding-3-small (1536 dims)
    firestore_service.py            # Firestore CRUD + KNN search + processed tracking
    grouping_service.py             # Three-layer clustering logic
```

## Env vars
```
# OpenAI
OPENAI_API_KEY=

# Slack
SLACK_BOT_TOKEN=
SLACK_SIGNING_SECRET=

# Gmail OAuth
GMAIL_CLIENT_SECRETS_FILE=         # path to client secrets JSON from Google Cloud Console
GMAIL_REDIRECT_URI=                 # e.g. https://<your-host>/auth/callback

# Email provider
EMAIL_PROVIDER=gmail                # gmail | fake | yahoo
DRY_RUN=true                        # true = no Gmail writes, logs as dry_run

# Firestore
GOOGLE_CLOUD_PROJECT=
FIRESTORE_DATABASE=(default)
GOOGLE_APPLICATION_CREDENTIALS=    # path to service account key JSON
```

## Gmail OAuth flow
1. `GET /auth/login` → redirects to Google consent screen
2. `GET /auth/callback` → exchanges code for token, writes `gmail_token.json`
3. All Gmail API calls auto-refresh via `gmail_token.json`

## Safety rules
1. `DRY_RUN=true` skips all Gmail write calls — logs action as `dry_run` status
2. Never delete — only archive (removeLabelIds: INBOX) or label
3. Every action logged to SQLite `action_logs` with `undo_status` tracking
4. Undo supported: `/undo <log_id>` reverses archive or label via `undo_action` tool

## Slack commands
- `/organize` — runs full two-phase workflow, posts markdown report
- `/digest` — concise summary of current groups + urgent items
- `/undo <id>` — reverses a specific action log entry

## API endpoints
- `GET /health` — status + config check
- `GET /auth/login` — start Gmail OAuth
- `GET /auth/callback` — complete Gmail OAuth
- `GET /logs` — action log (SQLite)
- `GET /groups` — all Firestore groups

## Firestore vector index (required for Layer 1)
Run once to enable KNN search:
```bash
gcloud firestore indexes composite create \
  --project=email-agent-dev-b6b5a \
  --collection-group=email_groups \
  --query-scope=COLLECTION \
  --field-config=vector-config='{"dimension":"1536","flat": "{}"}',field-path=embedding
```

## Running locally
```bash
# Terminal test (no Slack)
python test_agent.py

# Full app with Slack
uvicorn app_main:api --reload --port 8000
```

## Done when
- [x] Gmail OAuth (`/auth/login` → `/auth/callback` → `gmail_token.json`)
- [x] Three-layer group clustering (vector + structural + AI fallback)
- [x] Firestore groups + summaries
- [x] SQLite action logs with undo support
- [x] Slack `/organize`, `/digest`, `/undo`
- [x] `DRY_RUN=true` safety mode
- [x] Already-processed emails skipped on re-run
- [ ] Phase 1: `sync_gmail_labels` — bootstrap Firestore from existing Gmail labels
- [ ] Daily digest cron (8am)
- [ ] Yahoo IMAP provider implementation
