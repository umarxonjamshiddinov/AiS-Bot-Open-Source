# AiS-Bot

An open-source Telegram AI assistant built with Python and aiogram. The bot
connects to an AiS-compatible inference API, stores account and moderation data
in SQLite, and keeps conversational history in process memory.

[Русская версия](README_RU.md)

## Features

- Choose from the model catalog defined in `AI/models.py`, browse by provider,
  or select a random model. The API catalog is checked at startup. Availability
  still depends on the configured AiS API account.
- Text chat and photo analysis through the AiS API.
- Local text extraction from TXT, Markdown, code/text formats, CSV, TSV, XLSX,
  DOCX, and text-based PDFs before the selected model receives the content.
- Optional live web search through Tavily for current-information and explicit
  search requests, with returned source links.
- Required input moderation, automatic warnings and blocks, user appeals, and
  administrator moderation tools. Adaptive Safety shadow evaluation is
  separately configurable and does not apply shadow decisions as sanctions.
- Standard/Trusted access, Premium Models access, daily usage limits, and
  per-user overrides.
- SQLite persistence for account, usage, moderation, appeal, and audit data;
  automatic schema migrations and retention maintenance.

Audio and video analysis, scanned-PDF OCR, image generation, and direct
AWS/Azure inference are **not implemented**. Public inference and Safety
requests go through the configured AiS API.

## Architecture

See [Docs/architecture.md](Docs/architecture.md) for the request path and
storage boundaries. Model IDs and capabilities are maintained in
`AI/models.py`; only the visible catalog is exposed to users. The provider
selector retains an internal legacy identifier for stored settings, but the
runtime sends inference to AiS API, not directly to a cloud model provider.

## Technology

- Python 3.12 recommended
- aiogram 3.x and asyncio
- OpenAI Python SDK used as the AiS API transport
- SQLite with append-only migrations
- Tavily Python SDK for web search
- `unittest` for automated tests

Exact direct dependency versions are pinned in [`requirements.txt`](requirements.txt).

## Requirements

1. Python 3.12 (Python 3.10+ may work, but CI runs on 3.12).
2. A Telegram bot token from [@BotFather](https://t.me/BotFather).
3. An AiS API base URL and API key for an AiS-compatible backend. The backend
   must expose the exact public model IDs in `AI/models.py`, OpenAI-compatible
   model listing and chat-completion endpoints, and the AiS Safety assessment
   contract described below.
4. A Tavily API key. The current application requires it at startup because
   the web-search client is initialized even when a particular request does
   not trigger a search.

The project does not include an inference server or a database server. No
original production account is required if you supply your own compatible AiS
API service and credentials.

## Installation

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

### Linux or macOS

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set the required values. Never commit `.env`; `.gitignore`
excludes it and local runtime data.

## Configure a Telegram bot

1. Open [@BotFather](https://t.me/BotFather), create a bot with `/newbot`, and
   copy its token directly into the local `BOT_TOKEN` setting.
2. Add the numeric Telegram user ID(s) of trusted operators to
   `ADMIN_USER_IDS`, separated by commas. An empty value disables administrator
   access. Do not use a username as an authorization identifier.
3. Keep the token private. If it is exposed, revoke it with BotFather and
   replace it in the local environment.

The bot uses long polling. Run one active process for a given bot token and
database file.

## AiS API integration

Set `AIS_API_BASE_URL` to your own service URL and `AIS_API_KEY` to a key issued
by that service. Do not put credentials in the URL. The base URL may be the
service origin or its `/v1` path; the client normalizes it to `/v1`.

At startup the bot calls `GET /v1/models` and requires its returned IDs to
match the user-visible catalog in `AI/models.py`. Chat uses
`POST /v1/chat/completions`; image requests use the same endpoint with an image
input. Required moderation and shadow assessment use `POST
/v1/safety/assess`. That Safety API must implement the fixed `ais_safety_v1`
and `ais_safety_v2` contracts expected by `AI/safety_client.py`. The bot fails
closed when required Safety assessment is unavailable; do not disable
moderation as a workaround.

The model catalog is source-configured, not loaded from an environment
variable. To use a different backend catalog, update `AI/models.py` and its
tests to the backend's documented IDs and capabilities, then verify the API
contract before deployment. Model routing, provider credentials, and upstream
provider integrations belong to the AiS API service.

## Configuration reference

`BOT_TOKEN`, `AIS_API_KEY`, `AIS_API_BASE_URL`, and `TAVILY_API_KEY` are
required. All other variables are optional and use the defaults shown.
`.env.example` contains blank credential placeholders only.

| Variable | Default | Purpose |
|---|---:|---|
| `BOT_TOKEN` | — | Telegram bot token from BotFather. |
| `AIS_API_KEY` | — | Credential for the configured AiS API. |
| `AIS_API_BASE_URL` | — | AiS API origin or `/v1` base path; must be HTTP(S), without URL credentials, query, or fragment. |
| `TAVILY_API_KEY` | — | Tavily web-search credential; required by the current startup configuration. |
| `SUPPORT_CONTACT` | empty | Optional public contact text shown in support and access-denial messages. |
| `AI_MAX_CONCURRENT_REQUESTS` | `4` | Positive concurrency setting; the router requires at least `2`. |
| `AI_DEFAULT_MAX_OUTPUT_TOKENS` | `4096` | Default upper bound for model output, further limited by model metadata. |
| `AI_RETRY_COUNT` | `0` | Retained for compatibility; the bot does not replay ambiguous inference calls. |
| `AI_REQUEST_TIMEOUT_SECONDS` | `120` | AiS API request timeout. |
| `SAFETY_SHADOW_ENABLED` | `true` | Run adaptive Safety shadow evaluation. Shadow results do not independently sanction users; additional API usage may incur cost. |
| `SAFETY_SHADOW_QUEUE_SIZE` | `32` | Maximum in-memory shadow queue size. |
| `SAFETY_SHADOW_TIMEOUT_SECONDS` | `20` | Shadow assessment timeout setting. |
| `SQLITE_PATH` | `data/aiscloudbot.sqlite3` | SQLite file path; relative paths resolve from the project directory. |
| `SQLITE_BUSY_TIMEOUT_MS` | `5000` | SQLite busy timeout in milliseconds. |
| `ADMIN_USER_IDS` | empty | Comma-separated numeric Telegram user IDs allowed to administer the bot. |
| `USAGE_RETENTION_DAYS` | `90` | Retention period for eligible usage requests. |
| `MODERATION_RETENTION_DAYS` | `90` | Retention period for eligible moderation and Safety evaluations. |
| `AUDIT_RETENTION_DAYS` | `365` | Retention period for eligible administrator audit entries. |
| `STANDARD_DAILY_LIMIT` | `50` | Default Standard inference limit per UTC day. |
| `TRUSTED_DAILY_LIMIT` | `500` | Default Trusted limit; `0` means unlimited. Per-user overrides take precedence. |
| `HIGHER_COST_DAILY_LIMIT` | `15` | Shared daily limit for Higher-cost models. |
| `GROK_ACCESS_CODE` | empty | Optional code used to grant Premium Models access. |
| `PREMIUM_ACTIVATION_CODE` | empty | Optional code used to activate Trusted access. |
| `ACTIVATION_MAX_ATTEMPTS` | `5` | Failed activation attempts allowed in the configured window. |
| `ACTIVATION_WINDOW_SECONDS` | `900` | Activation-attempt counting window. |
| `ACTIVATION_COOLDOWN_SECONDS` | `900` | Cooldown after too many failed activation attempts. |

Use long, unique activation codes and store them only in the private runtime
environment. Empty activation-code settings disable the corresponding code
flow.

## Database and data handling

SQLite is created automatically at startup. Schema migrations are applied
automatically; upgrades from an existing schema create a verified backup
before migration. Keep the database on a writable, persistent **local**
filesystem that supports SQLite WAL mode. Do not share one SQLite file across
multiple bot instances or place it on an unsupported network filesystem.

Conversation history and FSM state live in process memory and are lost on
restart. Account settings, usage records, moderation events, appeals, and
administrator audit data are stored in SQLite subject to the configured
retention periods. Do not include production databases or backups in a source
distribution.

## Run locally

```sh
python main.py
```

On startup, the bot validates the AiS model catalog, opens SQLite, performs
maintenance, and starts Telegram long polling. Stop it with the normal
process interrupt so its clients and database are closed cleanly.

Run the automated checks with:

```sh
python -m compileall -q Admins AI Handlers Reader Search Services Storage Tests Utils app_context.py config.py main.py state.py
python -m unittest discover -s Tests -p "test_*.py" -v
python -m pip check
```

CI also runs Ruff, `pip-audit`, and Gitleaks. Live smoke scripts in `Tests/`
are intentionally separate; they may make real API calls and are not part of
the offline unit-test command.

## Deployment guidance

- Use a supported Python runtime, a process supervisor, a persistent local
  volume for `SQLITE_PATH`, and exactly one polling process per bot token.
- Inject secrets through the host's secret/environment manager; do not bake
  `.env` into images or check it into source control.
- Restrict filesystem access to the bot service account. Protect and
  periodically test backups.
- Use TLS for remote AiS API traffic. Review the AiS API and Tavily providers'
  data-handling terms before sending user content.
- No production deployment workflow, AWS Amplify project, webhook, or
  infrastructure configuration is included in this repository.

## Security and privacy

Never commit Telegram, AiS API, Tavily, or activation-code secrets. Treat
messages, uploaded files, photos, and model outputs as potentially sensitive:
they may be sent to the configured AiS API, and eligible requests may also be
sent to Tavily. Configure retention to match your needs and disclose processing
to bot users. Logs are designed to avoid raw credentials and conversation
content, but operators should still protect log files and review deployment
logging settings.

Administrator access is controlled by numeric IDs in `ADMIN_USER_IDS` and
admin operations are rechecked by the service layer. Moderation blocks,
restrictions, and appeals are persisted; access codes are not stored in user
records. The bot requires successful input Safety checks before inference.

## Troubleshooting

- **Missing environment variable:** set the required variables in `.env`; do
  not paste secrets into issue reports.
- **Startup fails with catalog mismatch:** check that the configured AiS API
  exposes exactly the visible model IDs in `AI/models.py` and that the key has
  access to them.
- **Authentication or Safety errors:** verify the API key, base URL, and the
  backend's chat and Safety contracts. The bot will not bypass Safety if its
  assessment endpoint is unavailable.
- **Database WAL or lock error:** use a writable local persistent path and
  ensure no second process is using the same database.
- **No admin controls:** set `ADMIN_USER_IDS` to numeric Telegram user IDs and
  restart the process.
- **Audio/video or scanned PDF:** these input types are not currently
  processed; use text, a supported document, or a photo instead.
- **Search unavailable:** verify `TAVILY_API_KEY` and network access. Ordinary
  chat can continue when a triggered web search returns no results.

## License and third-party notices

Original project code is licensed under the MIT License; see
[`LICENSE`](LICENSE). The license does not apply to third-party packages,
provider/model names, or external services. Direct runtime dependency
attributions and license identifiers are listed in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
