# Architecture & Workflow

> Last updated: May 12, 2026

---

## Table of Contents

1. [Project Structure](#project-structure)
2. [Layer Overview](#layer-overview)
3. [Workflows](#workflows)
   - [/organize — User-Triggered](#workflow-a----organize-user-triggered)
   - [Gmail Pub/Sub — Auto-Triggered](#workflow-b----gmail-pubsub-auto-triggered)
   - [/digest](#workflow-c----digest)
   - [/undo](#workflow-d----undo)
   - [Inbox Query (DM / @mention)](#workflow-e----inbox-query-dm--mention)
4. [Data Storage](#data-storage)
5. [Agent Routing Map](#agent-routing-map)
6. [Known Bottlenecks](#known-bottlenecks)

---

## Project Structure

```
.
├── app/
│   ├── server.py                    # FastAPI entry point + lifespan startup
│   └── routers/
│       ├── slack.py                 # Slack commands, DMs, modals, interactions
│       └── gmail_push.py            # Gmail Pub/Sub push endpoint
│
├── agent_service/
│   ├── main.py                      # ADK Runner + InMemorySessionService
│   └── email_agent/
│       ├── email_agent.py           # Root orchestrator agent (routing only)
│       │
│       ├── sub_agents/
│       │   ├── mailbox_sync_agent.py        # Syncs Gmail labels → Firestore
│       │   ├── inbox_processing_agent.py    # Classifies + groups emails
│       │   ├── summarization_agent.py       # Refreshes group summaries
│       │   ├── audit_agent.py               # Action log + undo
│       │   ├── digest_agent.py              # Daily digest (bypassed, see §Workflow C)
│       │   ├── inbox_query_agent.py         # Answers inbox stat questions
│       │   └── casual_agent.py              # Small talk + help
│       │
│       ├── tools/
│       │   ├── email_tools.py               # batch_process_emails, sync_gmail_labels
│       │   ├── digest_tools.py              # daily_digest
│       │   ├── inbox_query_tools.py         # get_inbox_stats, get_group_emails
│       │   ├── log_tools.py                 # get_action_log, undo_action
│       │   └── project_tools.py             # summarize_group, summarize_groups
│       │
│       ├── services/
│       │   ├── email_provider.py            # Gmail API singleton (OAuth + auto-refresh)
│       │   ├── firestore_service.py         # Firestore CRUD + vector KNN search
│       │   ├── grouping_service.py          # 3-layer email clustering
│       │   ├── embedding_service.py         # OpenAI text-embedding-3-small
│       │   ├── gmail_watch_service.py       # Pub/Sub watch registration + push handler
│       │   └── label_setup_service.py       # Empty label detection + user seeding
│       │
│       └── models/
│           └── action_log.py                # SQLite action log schema
│
└── Storage
    ├── Firestore                            # email_groups (vector DB), email_summaries
    ├── SQLite  (email_agent.db)             # action_logs for undo
    └── In-memory                            # ADK sessions (per-request context only)
```

---

## Layer Overview

```
┌─────────────────────────────────────────────────────────┐
│                     Slack / HTTP                         │
│         /organize  /digest  /undo  DM  @mention          │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────┐
│                  FastAPI  (app/)                          │
│   slack.py — commands, modals, Block Kit rendering        │
│   gmail_push.py — Pub/Sub push receiver                   │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────┐
│              Google ADK Agent Layer                       │
│                                                           │
│   root agent (email_agent.py)                             │
│     routes intent → sub-agent                             │
│                                                           │
│   sub-agents: mailbox_sync │ inbox_processing             │
│               summarization │ audit │ inbox_query         │
│               digest │ casual                              │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────┐
│                   Tools Layer                             │
│   batch_process_emails │ sync_gmail_labels                │
│   daily_digest │ get_inbox_stats │ undo_action            │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────┐
│                  Services Layer                           │
│   GmailProvider (singleton)  │  Firestore                 │
│   grouping_service (3-layer) │  embedding_service         │
│   gmail_watch_service        │  label_setup_service       │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────┐
│                     Storage                               │
│   Firestore  ── email_groups (vector), email_summaries    │
│   SQLite     ── action_logs                               │
│   Gmail      ── source of truth for inbox + labels        │
└─────────────────────────────────────────────────────────┘
```

---

## Workflows

### Workflow A — `/organize` (User-Triggered)

User types `/organize` in Slack. The full pipeline runs and posts a formatted Block Kit report.

```
Slack /organize
│
├── [slack.py] check for empty labels (find_empty_user_labels)
│     └── if found → post ephemeral message with "Set up & Organise" button
│                    user fills modal → seed Firestore → then continue below
│
└── _run_agent(task, user_id)  ← per-user asyncio lock (one run at a time)
      │
      └── root agent → identifies intent as ORGANIZE
            │
            ├── 1. mailbox_sync_agent
            │        sync_gmail_labels_if_needed()
            │          compare Gmail user labels vs Firestore groups
            │          if new labels found → embed + save to Firestore
            │
            ├── 2. inbox_processing_agent
            │        batch_process_emails(max_results=20)
            │          ┌── paginate Gmail (50/page, category:primary)
            │          │     stop when 20 unprocessed emails found
            │          │     (checks each page against Firestore to skip processed)
            │          │
            │          ├── group emails by sender domain
            │          │
            │          ├── GPT-4o-mini classify (1 call per domain batch)
            │          │     → group_name, should_archive, needs_attention
            │          │
            │          ├── per email → find_or_create_group (3-layer, see below)
            │          │
            │          ├── mark_email_processed → Firestore email_summaries
            │          ├── label_email → Gmail  (skipped in DRY_RUN)
            │          ├── archive_email → Gmail (skipped in DRY_RUN)
            │          ├── ActionLog → SQLite
            │          │
            │          └── GPT-4o-mini incremental summary update (1 call for ALL groups)
            │                input:  existing Firestore summary + new email snippets
            │                output: updated summary reflecting latest project status
            │                no body re-fetching — snippets already in memory
            │
            ├── 3. audit_agent
            │        confirms actions were logged
            │
            └── root agent compiles plain-text report (Slack mrkdwn)
                  │
                  └── _post_organize_result → Block Kit (header + chunked sections)
```

**3-Layer Grouping Logic** (`grouping_service.py`):

```
New email arrives with a proposed group_name
│
├── Layer 1 — Thread match
│     if thread_id already exists in a Firestore group → merge immediately
│
├── Layer 2 — Vector similarity (Firestore KNN, cosine distance)
│     similarity < 0.70  → create new group
│     similarity > 0.95  → merge into existing group
│     similarity 0.70–0.95 → go to Layer 3
│
└── Layer 3 — Structural signals (score ≥ 2 → merge, else AI)
      +1 if same thread_id
      +1 if same sender
      +1 if group name overlaps
      +1 if group active within 30 days
        │
        └── if score < 2 → GPT-4o-mini decides: "join" or "new"
```

---

### Workflow B — Gmail Pub/Sub (Auto-Triggered)

Fires automatically when a new email arrives in Gmail Primary. No user action needed.

```
New email lands in Gmail inbox
│
Google Cloud Pub/Sub
│
POST /gmail/push
│
process_push_notification(payload)   ← pure Python, no agent, no session
  │
  ├── decode historyId from Pub/Sub payload
  ├── fetch Gmail history since last stored cursor
  │
  ├── for each new message (messagesAdded):
  │     skip if already in Firestore (get_processed_email_ids)
  │     GPT-4o-mini classify (1 call per email — no batching)
  │     find_or_create_group → Firestore
  │     mark_email_processed → Firestore
  │     label_email → Gmail  (skipped in DRY_RUN)
  │
  ├── for each manual label change (labelsAdded):
  │     detect user-created label
  │     find_or_create_group → Firestore
  │     mark_email_processed → Firestore
  │
  └── update history cursor in Firestore
        always return 200 (prevents Pub/Sub retries)

Watch renewal:
  server startup → renew_if_needed()
    if < 24h remaining → call Gmail watch() API → reset to 168h
```

---

### Workflow C — `/digest`

Bypasses the agent entirely. Calls the tool directly for reliable structured output.

```
Slack /digest
│
└── [slack.py] asyncio.to_thread(daily_digest)   ← no LLM involved
      │
      ├── list_groups() from Firestore
      ├── sort by last_activity (most recent first)
      └── return {group_count, total_emails, groups[]}
            │
            └── _post_digest_result → Block Kit
                  header: "📧 Daily Digest — {date}"
                  stats:  "*N groups* · *M emails* organised"
                  divider
                  ⚠️ Needs Attention  (groups whose summary contains
                     keywords: urgent, overdue, fault, safety, action, deadline)
                  divider
                  📁 Groups (sorted by recent activity, summary truncated to 90 chars)
```

---

### Workflow D — `/undo`

```
Slack /undo <log_id>
│
└── _run_agent("Undo action log entry #N", user_id)
      │
      └── root agent → audit_agent
            │
            ├── /undo <id>          → undo_action(log_id=N) directly
            ├── "undo last action"  → undo_last_action()
            └── "undo the invoice"  → preview_undo(description=...)
                                       show candidates → user confirms
                                       undo_action(log_id=confirmed)
```

Undo reverses:
- `archive` → `unarchive_email` (adds INBOX label back in Gmail)
- `label` → `remove_label` (removes label from Gmail)

---

### Workflow E — Inbox Query (DM / @mention)

```
User DMs bot or @mentions in channel
│
└── _run_agent(user_message, user_id)
      │
      └── root agent → inbox_query_agent
            │
            ├── "how many groups?"      → get_inbox_stats()
            ├── "show Siemens emails"   → get_group_emails("Siemens")
            └── "summary of group X"   → get_inbox_stats() or get_group_emails(X)

            All reads are against Firestore — no Gmail API calls.
```

---

## Data Storage

### Firestore Collections

| Collection | Key | Fields |
|---|---|---|
| `email_groups` | `group_id` (8-char hex) | name, description, summary, embedding (1536-dim), email_ids[], senders[], thread_ids[], email_count, last_activity, source |
| `email_summaries` | `email_id` (Gmail message ID) | group_id, processed, subject, sender, date, snippet |
| `_config/gmail_watch` | fixed | history_id, expiration, registered_at |

### SQLite (`email_agent.db`)

| Table | Purpose |
|---|---|
| `action_logs` | Every label/archive action with user, email_id, subject, status, timestamp — used by `/undo` |

### In-Memory (ADK Session)

| Key | Value | Lifetime |
|---|---|---|
| `user_id` | Slack user ID | Session (resets on restart) |
| `user_name` | Slack display name | Session |
| `dry_run` | bool from `DRY_RUN` env var | Session |
| `interaction_history` | list of organize runs | Session |

> **Note:** Sessions use `InMemorySessionService` — state resets on server restart. All durable data lives in Firestore or SQLite.

---

## Agent Routing Map

| User intent | Routed to |
|---|---|
| `/organize`, "sort my inbox", "process emails" | `mailbox_sync` → `inbox_processing` (includes summary) → `audit` |
| `/digest`, "daily summary" | `digest_agent` (or direct tool call from Slack) |
| `/undo <id>`, "undo that" | `audit_agent` |
| "how many groups?", "show group X" | `inbox_query_agent` |
| greetings, thanks, small talk | `casual_agent` |

---

## Known Bottlenecks

| # | Bottleneck | Impact | Location |
|---|---|---|---|
| 1 | **Gmail metadata fetches are 1-by-1** | 1 list + 50 individual GETs per page. Finding 20 new emails in a 2000-email inbox can cost 150+ API calls | `email_provider.py: fetch_emails_page` |
| 2 | **Firestore processed-check is sequential** | 50 individual `.get()` reads per page instead of a batched lookup | `firestore_service.py: get_processed_email_ids` |
| 3 | **Embedding + KNN per email** | 1 OpenAI embedding call + 1 Firestore KNN search per email × 20 emails = 40 API calls just for grouping | `grouping_service.py: find_or_create_group` |
| 4 | **Summarization uses snippets, not full bodies** | Incremental summary uses 150-char snippets (already in memory). Full body quality is lower but token cost is ~10x less. Re-introduce body fetch only if summary quality is insufficient | `email_tools.py: batch_process_emails step 5` |
| 5 | **Pub/Sub classifies one email at a time** | Burst of 5 emails = 5 sequential GPT calls. No batching unlike `/organize` | `gmail_watch_service.py: process_push_notification` |
| 6 | **Per-user lock blocks all commands** | `/organize` takes 30–60s. DMs queue silently behind the lock with no user feedback | `slack.py: _run_agent` |
| 7 | **Root agent LLM call on every command** | Even if `batch_process_emails` returns "nothing new", the full 4-step pipeline still runs 4 sub-agent LLM routing calls | `email_agent.py` |
