# Architecture

## Runtime flow

1. `main.py` loads and validates environment configuration, creates the
   aiogram bot and dispatcher, wires middleware and handlers, and starts
   polling.
2. `Handlers/` handles Telegram commands, callbacks, text, photos, and
   supported documents. Authentication and control-flow middleware apply
   before handlers run.
3. `Services/` coordinates account access, usage reservations, moderation,
   notifications, and request processing.
4. `AI/` owns the static model catalog, prompt construction, model transport,
   input moderation, adaptive Safety policy, and provider compatibility
   metadata. Public model requests use the configured AiS API; the bot does
   not call AWS or model providers directly.
5. `Search/` invokes Tavily only when a request matches the search heuristics.
   `Reader/` extracts text locally from supported uploaded files.
6. `Storage/` implements SQLite repositories, schema migrations, runtime
   locking, verified migration backups, retention, and audit persistence.

## AiS API contract

The bot validates the public model listing at startup, sends text and image
requests through the OpenAI-compatible chat-completion API, and uses the
AiS-owned `safety/assess` endpoint for required input moderation and optional
shadow assessment. Model routing and upstream provider credentials belong to
the AiS API deployment.

## State and privacy boundaries

- Conversation history and Telegram FSM state are in process memory.
- Account configuration, usage, moderation decisions, appeals, and audit data
  are persisted in SQLite.
- SQLite uses WAL and is protected by a runtime lease; deploy one process per
  database file on a local persistent filesystem.
- Logs use safe metadata rather than raw prompt contents or credential values.
- A web search, when triggered, sends the search query to Tavily. Model and
  Safety calls send relevant user content to the configured AiS API.

## Directory map

| Path | Responsibility |
|---|---|
| `Admins/` | Administrator panel presentation and service behavior. |
| `AI/` | Model metadata, inference transport, moderation, and Safety logic. |
| `Docs/` | Public architecture and model-access notes. |
| `Handlers/` | Telegram command, message, media, and callback handlers. |
| `Reader/` | Local document text extraction. |
| `Search/` | Tavily search and source handling. |
| `Services/` | Application-level user, request, moderation, and notification services. |
| `Storage/` | SQLite schemas, migrations, repositories, and maintenance. |
| `Tests/` | Offline unit/integration tests and separately run live smoke scripts. |
| `Utils/` | Logging, privacy redaction, and Telegram formatting helpers. |
