# Report Agent

A self-hosted web app where a user chats with a Claude agent that queries Snowflake,
drafts an analytical report, and publishes it as a PDF with a download link, all in
one container. It is the local VS Code + Claude Code reporting setup, packaged as a
service: same engine (the Claude Agent SDK runs the Claude Code binary in-process),
same `CLAUDE.md` instructions, same transforms repository for schema context, with
Amazon Bedrock as the model provider.

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
workspace/              What the agent sees as its project
  CLAUDE.md             ← put your AGENTS.md content here (or make it `@AGENTS.md`)
  README.md             ← your data notes
  transforms/           ← git submodule of your transforms repo
infra/                  ECS Fargate task definition + IAM task-role policy
tests/                  SQL guard, PDF render, and an end-to-end WebSocket round-trip (mock mode)
```

## Run locally in 60 seconds (no credentials)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip3 install -r requirements.txt -r requirements-dev.txt
pytest -q
uvicorn app.main:app --reload --port 8080      # AGENT_MODE=mock by default
```

Open http://localhost:8080, type "generate the report", and a real PDF is produced
from mock data through the real `publish_report` tool. This exercises everything
except the model.

## Run against Bedrock

```bash
cp .env.example .env            # fill in Snowflake + model; keep REPORT_STORAGE=local for now
export AWS_PROFILE=your-profile # any credential chain works: profile, SSO, env vars, or AWS_BEARER_TOKEN_BEDROCK
set -a; . ./.env; set +a
AGENT_MODE=sdk uvicorn app.main:app --port 8080
```

or `docker compose up --build` (mounts `~/.aws` read-only into the container).

Verify both dependencies before starting the app:
`python scripts/check_snowflake.py` and `python scripts/check_bedrock.py`.

Bedrock prerequisites: model access enabled in the account, and the caller allowed
`bedrock:InvokeModel*` plus `bedrock:ListInferenceProfiles`/`GetInferenceProfile`
(see `infra/iam-task-role-policy.json`). Use a cross-region inference profile ID
(`us.anthropic.claude-…`) in `ANTHROPIC_MODEL`. WebSearch is not available via Bedrock;
this app does not enable it.

## Bring your local setup over

1. **Instructions.** Copy your `AGENTS.md` into `workspace/CLAUDE.md`. Claude Code reads
   `CLAUDE.md`; if you prefer to keep the file name, make `CLAUDE.md` a single line: `@AGENTS.md`.
   The system prompt appended in `app/agent.py` (`SYSTEM_APPEND`) describes the tools; keep
   report-content guidance in `CLAUDE.md`.
2. **Data notes.** Copy your `README.md` into `workspace/README.md`.
3. **Transforms.** `git submodule add <url> workspace/transforms`. The agent explores it with
   Glob/Grep/Read exactly as it does locally.
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

## Deploying on AWS (ECS Fargate)

```bash
git submodule update --init
docker build -t report-agent .
aws ecr get-login-password | docker login --username AWS --password-stdin ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com
docker tag report-agent ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/report-agent:latest && docker push ...
aws ecs register-task-definition --cli-input-json file://infra/ecs-task-definition.json
```

Then a Fargate service behind an ALB. Put authentication on the ALB listener (Cognito or any
OIDC IdP): the app reads the user's identity from `X-Forwarded-User` / `X-Auth-Request-Email`
for attribution and does no auth of its own. Enable WebSocket support (ALB does by default;
idle timeout ≥ 5 min recommended). Set `REPORT_STORAGE=s3` with a private bucket; download
links are presigned for `REPORT_URL_TTL_S`.

**Scaling note.** Sessions are in-memory, so run one task or enable ALB sticky sessions.
For horizontal scale, persist `session_id` from the `result` event and reconnect with
`ClaudeAgentOptions(resume=...)`; the SDK also offers `SessionStore` for this.

## Security posture

- Model has no Bash, Write, Edit, or network tools. It can only read `workspace/` and call
  the four custom tools.
- SQL is validated read-only and row-capped in code; the Snowflake role must also be read-only.
- Container runs as a non-root user; secrets come from Secrets Manager, never the image.
- `AGENT_MAX_TURNS` and `AGENT_MAX_BUDGET_USD` bound each turn.
- Reports are written to a private bucket; links expire.

## Configuration

See `.env.example`. Every variable has a working default for local development
(`AGENT_MODE=mock`, `SNOWFLAKE_MODE=mock`, `REPORT_STORAGE=local`).

## Tests

```bash
pytest -q
```

`tests/test_api.py` drives a full conversation over the WebSocket with the mock agent and
asserts a valid PDF is served back.
