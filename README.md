# Report Agent

A self-hosted web app where a user chats with a Claude agent that queries Snowflake,
drafts an analytical report, and publishes it as a PDF with a download link, all in
one container. It is the local VS Code + Claude Code reporting setup, packaged as a
service: same engine (the Claude Agent SDK runs the Claude Code binary in-process),
same `CLAUDE.md` instructions, with Amazon Bedrock as the model provider. Schema
context comes from a generated **Query Context Pack** rather than the raw transforms
repo — see `docs/catalog-design.md` for why.

```
browser ──ws──> FastAPI ──> ClaudeSDKClient (one per conversation)
                   │              │  built-in: Read / Glob / Grep over workspace/
                   │              │  custom  : run_sql · list_tables · describe_table · publish_report
                   │              ▼
                   │         Bedrock (Claude)          Snowflake (read-only role)
                   │
                   └── publish_report ──> Markdown → HTML → PDF (WeasyPrint) ──> S3 presigned URL
                                                                                 (or local /reports/)
```

## How this works under the hood
The ClaudeSDKClient package can spawn Claude Code CLI processes which act as the orchestrator (the agent loop and deciding which tool to call next). That runs within a container, having access to certain set of tools and files.
 
* Built in Tools: Read/Glob/Grep
* Custom tools: run_sql, list_tables, describe_table, publish_report (in-process MCP server)
* (Anything outside this list of tools is denied by default - i.e. no bash, no write, and no network)
* Files Loaded every conversation - claude.md plus its two @ imports qcp/README.md and qcp/index.md
* Files reachable on demand: Everything in /workspace is available to read/glob/grap, and nothing outside that folder.


## Layout

```
app/
  main.py               FastAPI: chat UI, /conversations, WebSocket, /reports download
  agent.py              Session manager; SDK options; message → UI event translation; mock agent
  config.py             All settings, from env vars
  pdf.py                Markdown → PDF
  storage.py            Local disk or S3 (+presigned URLs)
  tools/snowflake_sql.py  run_sql / list_tables / describe_table (read-only guard, row cap, timeout)
  tools/publish_report.py publish_report tool (renders, stores, notifies the UI)
static/index.html       Single-file chat UI (streaming text, tool activity, download cards)
app/db/                 Persistence: engine, repository, event writer, SDK session store
alembic/                Migrations (`alembic upgrade head`)
workspace/              What the agent sees as its project
  CLAUDE.md             Agent instructions; @-imports the pack's README and index
  README.md             Hand-written data notes the pack cannot supply
  qcp/                  Generated Query Context Pack — gitignored, build it (below)
scripts/
  check_snowflake.py      Connectivity check for the SNOWFLAKE_* settings
  check_bedrock.py        Connectivity check for Bedrock model access
  query_context_pack_extractors/
    phrase_data_model/    Builds the pack from Snowflake + the snowflake-etl dbt manifest
docs/
  query-context-pack-spec.md  The pack format, database-agnostic
  catalog-design.md           Why the pack exists and what is in each tier
infra/                  ECS task definition + IAM task-role policy (reference only)
tests/                  SQL guard, PDF render, and an end-to-end WebSocket round-trip (mock mode)
```

## Run locally in 5 minutes (no model or warehouse credentials)

Persistence is required — the app records every conversation, turn and report — so
this needs a Postgres, but nothing else.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip3 install -r requirements.txt -r requirements-dev.txt

docker compose up -d postgres                  # POSTGRES_PORT=5433 if 5432 is taken
export DATABASE_URL=postgresql+asyncpg://report_agent:report_agent@localhost:5432/report_agent
alembic upgrade head

pytest -q
uvicorn app.main:app --reload --port 8080      # AGENT_MODE=mock unless .env says otherwise
```

Without `DATABASE_URL` the app refuses to start and tells you these commands, rather
than falling back to memory and looking like it worked. The database tests skip with
the same message when nothing is listening on 5432.

Open http://localhost:8080, type "generate the report", and a real PDF is produced
from mock data through the real `publish_report` tool. This exercises everything
except the model.

## Run locally with credentials

Three things to set up, in this order: the model, the database, and the Query Context
Pack the agent uses to find tables. Each has a check you can run before moving on.

### 1. Credentials

```bash
cp .env.example .env
```

Fill in three groups:

| group | what it needs |
|---|---|
| **Bedrock** | `AGENT_MODE=sdk`, `CLAUDE_CODE_USE_BEDROCK=1`, `AWS_REGION`, `ANTHROPIC_MODEL` (a cross-region inference profile id, `us.anthropic.claude-…`). Credentials come from any AWS chain: `AWS_PROFILE`, SSO, env vars, or `AWS_BEARER_TOKEN_BEDROCK`. |
| **Snowflake** | `SNOWFLAKE_MODE=real`, plus `ACCOUNT`, `USER`, `ROLE`, `WAREHOUSE`, `DATABASE`. For key-pair auth put the key in `secrets/` (git-ignored) and set `SNOWFLAKE_PRIVATE_KEY_PATH`. Give the role `SELECT` only. |
| **Pack build** | `SNOWFLAKE_ETL_DIR` — your `snowflake-etl` checkout, read for dbt manifests. |
| **Database** | `DATABASE_URL`. Required. See above. |

`.env` is loaded by `app/config.py`, so a bare `uvicorn` picks it up; real environment
variables still win, which keeps `AGENT_MODE=mock uvicorn ...` working.

Bedrock prerequisites: model access enabled in the account, and the caller allowed
`bedrock:InvokeModel*` plus `bedrock:ListInferenceProfiles`/`GetInferenceProfile`
(see `infra/iam-task-role-policy.json`). WebSearch is not available via Bedrock; this
app does not enable it.

### 2. Verify both connections

```bash
python scripts/check_snowflake.py     # auth, grants, and a real SELECT
python scripts/check_bedrock.py       # boto3 Converse, then the Agent SDK path
```

Both print which stage failed and why. Do not skip these — a bad inference-profile id
and an ungranted role fail at the same place in the UI but need different fixes.

### 3. Build the minimum Query Context Pack
The Query Context Pack is a digested set of data that helps the application navigate the databases of interest. The agent finds tables through `workspace/qcp/`. It is generated and git-ignored, so a fresh clone has none and `workspace/CLAUDE.md` will import files that do not exist.

**The minimum is conformance level L0** — every relation, column and type, from
`INFORMATION_SCHEMA` alone. No dbt manifest, no table scans, a few seconds:

```bash
python scripts/query_context_pack_extractors/phrase_data_model/build.py \
    --stage introspect --stage render --stage validate
```

That is enough for the app to run and for the agent to stop guessing column names. Add
the rest as it becomes worth it:

| level | command adds | gives the agent | cost |
|---|---|---|---|
| **L0** | *(the minimum above)* | what exists, columns, types | metadata only |
| **L2** | `--stage transforms` | descriptions, grain, lineage, the Clarity crosswalk | reads the dbt manifest |
| **L3** | `--freshness` | row counts and latest business date; `EMPTY` / `STALE` markers | one scan per relation |
| **L4** | `--joins --profiles` | verified join paths, value distributions | expensive; read the extractor README first |

L2 needs a parsed manifest in your `snowflake-etl` checkout:

```bash
cd $SNOWFLAKE_ETL_DIR/epic/transforms && make parse
```

`build.py` with no flags runs introspect + transforms + render + validate — L2, and the
sensible default. `--stage render` alone re-renders from cached facts without querying
anything. See `scripts/query_context_pack_extractors/phrase_data_model/README.md`.

### 4. Run

```bash
uvicorn app.main:app --port 8080
```

or `docker compose up --build`, which mounts `~/.aws` read-only into the container. The
image ships `workspace/` including the pack, so **build the pack before `docker build`** —
the build fails with a clear message if you do not.

On macOS, PDF rendering needs WeasyPrint's native libraries: `brew install pango`
(`app/pdf.py` adds the Homebrew prefix to the dynamic-loader path for you).

### Keeping the pack current

The pack is a build artifact, not source. It goes stale when the warehouse changes, and
`MANIFEST.md` records `built_at` and a `coverage` block so you can see how thin it is.
Rebuilding L0–L2 is cheap and safe to run often; `--freshness` costs scans, so it suits a
scheduled job rather than every start.

## Bring your local setup over

1. **Instructions.** `workspace/CLAUDE.md` already wires up the pack — keep its
   `@qcp/README.md` and `@qcp/index.md` imports and add your own report-content guidance
   around them. The system prompt appended in `app/agent.py` (`SYSTEM_APPEND`) describes
   the tools.
2. **Data notes.** Copy your `README.md` into `workspace/README.md`.
3. **Schema context.** Nothing to copy — build the Query Context Pack (above). It replaces
   the transforms repo: `workspace/qcp/sources/` holds the model definitions the agent may
   need, and everything else it needs is indexed. Query strategy worth writing down goes in
   `workspace/qcp/concepts/<slug>.md`, which survives pack rebuilds.
4. **Snowflake.** Your local script is replaced by the `run_sql` tool (`app/tools/snowflake_sql.py`).
   It connects with `snowflake-connector-python` using password or key-pair auth, forces a
   `STATEMENT_TIMEOUT`, rejects anything but a single `SELECT`/`WITH`, and caps rows. Give the
   `SNOWFLAKE_ROLE` only `SELECT` grants; the guard is belt, the role is braces.
   If you want the agent to reuse logic from your script (connection quirks, helper views),
   port it into `SnowflakeBackend`.
   For key-pair auth, drop the private key in `secrets/` (git-ignored; see `secrets/README.md`)
   and verify the whole chain with `python scripts/check_snowflake.py` before starting the app.

## How a turn flows

1. Browser `POST /conversations` → server creates an `AgentSession` (a `ClaudeSDKClient`
   with `cwd=workspace/`, `setting_sources=["project"]` so `CLAUDE.md` loads, built-in tools
   limited to Read/Glob/Grep, custom tools on an in-process MCP server, `permission_mode="dontAsk"`
   so anything not in `allowed_tools` is denied, never prompted).
2. Browser opens `WS /ws/{id}` and sends `{"prompt": ...}`.
3. The server iterates `client.receive_response()` and forwards compact events:
   `text_delta`, `assistant_text`, `tool_use`, `tool_result`, `report`, `result`, `error`.
4. When the model calls `publish_report`, the tool renders the PDF, stores it, and pushes a
   `report` event (title, URL, filename) which the UI renders as a download card. The link
   is generated by the server, not by the model, so it is always real.
5. The session stays alive for follow-ups ("break that down by facility") until idle for
   `SESSION_IDLE_TTL_S`.

## Security posture

- Model has no Bash, Write, Edit, or network tools. It can call the four custom tools,
  and its file tools are confined to `workspace/` by a `PreToolUse` hook
  (`app/workspace_guard.py`) — `allowed_tools` gates tool *names*, not paths, and `cwd`
  only decides where relative paths resolve, so neither is a boundary on its own. This
  matters because `run_sql` puts free-text clinician content into the model's context,
  and the container holds the Snowflake key and AWS credentials.
- SQL is validated read-only and row-capped in code; the Snowflake role must also be read-only.
- A conversation is bound to one database when it starts and cannot change it. The
  restriction is applied to every statement, not just the connection, because a
  fully-qualified name bypasses the connection's default.
- Container runs as a non-root user; secrets never go in the image.
- The pack ships in the image and contains schema metadata, not data — except
  `qcp/profiles/`, which carries sample column values and is only built on request.
- `AGENT_MAX_TURNS` and `AGENT_MAX_BUDGET_USD` bound each turn.
- Reports are written to a private bucket; links expire.
- Conversations, turns, the event log and reports are persisted (see
  `docs/persistence-schema.md`). The SDK transcript is mirrored to Postgres so a
  conversation can be resumed after a restart; `CLAUDE_CONFIG_DIR` is scratch.

## Configuration

See `.env.example`. Every variable has a working default for local development
(`AGENT_MODE=mock`, `SNOWFLAKE_MODE=mock`, `REPORT_STORAGE=local`).

## Tests

```bash
pytest -q
```

`tests/test_api.py` drives a full conversation over the WebSocket with the mock agent and
asserts a valid PDF is served back.
