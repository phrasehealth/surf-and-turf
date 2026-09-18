# Persistence schema

**Status:** proposed
**Scope:** what the report agent stores in Postgres, and why
**Prerequisite:** there is no database today. `SessionManager.sessions` is an
in-memory dict, and the only durable artefacts are the PDFs in `data/reports/`
(or S3). This introduces a new dependency.

---

## 1. What this is for

Four things at once, and the first is what shapes the design.

**Reuse.** Refresh a prior report against new data; assemble a report from analyses
picked from anywhere; clone an analysis onto a different cohort. These require an
analysis to be a *reproducible specification*, not a record of what happened.

**Replay.** Reopen a past conversation with every message and every agent step, so a
number can be traced back to the exchange that produced it.

**Numbering.** A durable serial for conversations, reports and figures.

**Audit.** Questions that are currently unanswerable:

- *Which reports were built on the catalogue from before the schema change?*
- *Which reports cite `gold.flowsheet_catalog`, whose latest date is 2001-08-15?*
- *Who asked for this number, when, and what SQL produced it?*
- *Which published reports used a join we never verified?*

Every one of those is a question about a report that has already gone to someone.
The schema is designed around them, and the numbering falls out of it.

---

## 2. The shape of the domain

Four facts drive the design. The first three are how the system behaves today; the
fourth is what the reuse features demand, and it is the one that changes everything.

**A conversation accumulates analyses like a shopping cart.** `CLAUDE.md` tells the
agent that a further request *adds* an analysis rather than replacing the report.

**Publishing is repeatable and produces an immutable artefact.** Each
`publish_report` call renders a new PDF. A user who adds a fourth analysis and
republishes gets a second file; the first may already have been sent to someone.

**A report is therefore a snapshot, not a container.** That is why `report_contents`
exists rather than `analyses.report_id`: without it, republishing would silently
rewrite history that a PDF in someone's inbox still reflects.

**An analysis must be a recipe, not a receipt.** This is the consequence of the three
reuse features, and it is a different data model from an audit log:

| feature | what it needs |
|---|---|
| refresh a report with new data | re-execute each analysis — so the *how* must be stored, not just the result |
| build a report from arbitrary analyses | analyses addressable independently of the conversation that produced them |
| clone an analysis with different filters | filters stored as named, swappable parameters — not baked into a SQL string |

So `analyses` holds a **specification** (a SQL template, named parameters, a chart
spec) and `analysis_runs` holds each **execution** of it. A report snapshots *runs*,
because a report shows the numbers as they were when it was published — refreshing
produces new runs and a new report, and the old PDF stays true to its own runs.

### Two consequences worth deciding before building

**The numbering scheme does not survive feature 2.** A label of
`[conversation]-[report]-[figure]` assumes a report belongs to one conversation. If a
user assembles a report from analyses drawn from conversations 12, 47 and 103, level
one has no answer. Containment is what a label can encode, and once analyses are
reusable the only real containment is *report contains figure*. The schema below
gives reports and analyses their own global serials — so a figure is `R-88-03`, an
analysis is `A-1042` and can be cited when picking one, and the originating
conversation is recorded as provenance rather than as an address.

**The agent does not currently emit any of this.** Its only structured output is
`publish_report(title, subtitle, body_markdown)` — prose. A specification with named
parameters cannot be recovered from that by parsing, reliably or at all. Features 1
and 3 are therefore blocked on a new tool (`record_analysis`) through which the agent
declares each analysis as it goes: template, parameters, chart spec. **That is a
change to the agent, not to the database**, and it is the real prerequisite here.

## 3. Schema

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()

-- ---------------------------------------------------------------- conversations
-- Provenance, not containment: where an analysis was born, not where it lives.
CREATE TABLE conversations (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    serial            bigint NOT NULL GENERATED ALWAYS AS IDENTITY UNIQUE,
    user_id           text,                       -- X-Forwarded-User
    sdk_session_id    text,                       -- ResultMessage.session_id, for log correlation
    started_at        timestamptz NOT NULL DEFAULT now(),
    last_used_at      timestamptz NOT NULL DEFAULT now(),
    closed_at         timestamptz,
    -- ResultMessage.total_cost_usd is a running conversation total, not per-turn.
    total_cost_usd    numeric(10,4),
    total_turns       integer
);
CREATE INDEX ON conversations (user_id, started_at DESC);

-- --------------------------------------------------------------------- analyses
-- A SPECIFICATION: enough to run it again, against other dates or another cohort.
CREATE TABLE analyses (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Global and human-facing, so a user can cite one when assembling a report.
    serial              bigint NOT NULL GENERATED ALWAYS AS IDENTITY UNIQUE,
    origin_conversation_id uuid REFERENCES conversations(id) ON DELETE SET NULL,
    -- Feature 3: a clone records what it was cloned from, so a family of cohort
    -- variants stays legible instead of looking like unrelated analyses.
    derived_from_id     uuid REFERENCES analyses(id) ON DELETE SET NULL,
    title               text NOT NULL,
    subtitle            text,
    note_template       text,        -- footnote text; parameters interpolated at run time
    -- The query as a template with :named parameters, never with literals baked in.
    -- Swapping a cohort means rebinding a parameter, not rewriting SQL.
    sql_template        text NOT NULL,
    chart_type          text,        -- 'hbar' | 'vbar' | 'stacked' | 'stat_tiles' | NULL for table-only
    chart_spec          jsonb,       -- which result columns map to which channel
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text,
    archived_at         timestamptz  -- hidden from pickers; never deleted, runs reference it
);
CREATE INDEX ON analyses (created_by, created_at DESC);
CREATE INDEX ON analyses (derived_from_id);

-- The swappable parts. `expression` is the intent, `value` the current binding:
-- a refresh re-resolves "last 12 months", an absolute range is left alone.
CREATE TABLE analysis_parameters (
    analysis_id  uuid NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    name         text NOT NULL,                  -- ':date_from', ':dx_codes'
    kind         text NOT NULL,                  -- 'date'|'date_range'|'code_list'|'scalar'|'identifier'
    value        jsonb NOT NULL,                 -- the resolved binding
    expression   text,                           -- 'last 12 months', 'ICD-10 D66*'
    label        text,                           -- what to call it in the UI: "Patient population"
    PRIMARY KEY (analysis_id, name)
);

-- Relations the specification reads. Declared, so "which analyses touch this table"
-- is answerable without executing anything.
CREATE TABLE analysis_relations (
    analysis_id   uuid NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    schema_name   text NOT NULL,
    relation_name text NOT NULL,
    PRIMARY KEY (analysis_id, schema_name, relation_name)
);

CREATE TABLE analysis_joins (
    analysis_id uuid NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    left_ref    text NOT NULL,                   -- 'gold.alert_events.alert_id'
    right_ref   text NOT NULL,
    verified    boolean NOT NULL,                -- FALSE = the agent's own hypothesis
    match_pct   numeric(5,2),
    PRIMARY KEY (analysis_id, left_ref, right_ref)
);

-- ---------------------------------------------------------------- analysis_runs
-- One execution. Feature 1 (refresh) creates new rows here, never edits old ones.
CREATE TABLE analysis_runs (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_id    uuid NOT NULL REFERENCES analyses(id) ON DELETE RESTRICT,
    ran_at         timestamptz NOT NULL DEFAULT now(),
    ran_by         text,
    -- The template with this run's bindings applied, as executed.
    resolved_sql   text NOT NULL,
    bound_params   jsonb NOT NULL,               -- snapshot: what the params were THIS time
    database_name  text NOT NULL,
    role_name      text,
    duration_ms    integer,
    row_count      integer,
    error          text,
    -- Provenance of the schema knowledge, and of the data's own currency.
    qcp_built_at   timestamptz,
    qcp_conformance text,
    freshness      jsonb,                        -- {'gold.alert_events': 'fresh 2026-09-15', …}
    -- Lets a refresh answer "did anything actually change?" without keeping rows.
    result_digest  text,
    -- No result column: results are PHI. See §5.
    resolved_note  text                          -- the footnote as rendered for this run
);
CREATE INDEX ON analysis_runs (analysis_id, ran_at DESC);

-- ---------------------------------------------------------------------- reports
-- One row per publish. Immutable: the PDF may already have been sent.
CREATE TABLE reports (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Global, because a report may be assembled from several conversations (or none).
    serial            bigint NOT NULL GENERATED ALWAYS AS IDENTITY UNIQUE,
    -- Nullable on purpose: an assembled report belongs to no single conversation.
    origin_conversation_id uuid REFERENCES conversations(id) ON DELETE SET NULL,
    -- Feature 1: a refresh points back at what it refreshed.
    refresh_of_id     uuid REFERENCES reports(id) ON DELETE SET NULL,
    title             text NOT NULL,
    subtitle          text,
    storage_backend   text NOT NULL,             -- 'local' | 's3'
    storage_key       text NOT NULL,
    report_uid        text NOT NULL UNIQUE,      -- StoredReport.report_id, in the filename
    size_bytes        integer,
    published_at      timestamptz NOT NULL DEFAULT now(),
    published_by      text,
    handling_marking  text,
    body_markdown     text                       -- see §5 before populating
);
CREATE INDEX ON reports (published_at DESC);
CREATE INDEX ON reports (refresh_of_id);

-- What this publish contained. Points at RUNS, not analyses: a report shows the
-- numbers as they were, and a later refresh must not retouch it.
CREATE TABLE report_contents (
    report_id       uuid NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    analysis_run_id uuid NOT NULL REFERENCES analysis_runs(id) ON DELETE RESTRICT,
    position        integer NOT NULL,
    PRIMARY KEY (report_id, analysis_run_id),
    UNIQUE (report_id, position)
);

-- ---------------------------------------------------------------------- figures
CREATE TABLE figures (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id       uuid NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    analysis_run_id uuid REFERENCES analysis_runs(id) ON DELETE SET NULL,
    serial          integer NOT NULL,            -- position within this report
    -- Denormalised deliberately: the label is printed in a PDF someone may hold,
    -- so it must not change because a parent row was edited.
    label           text NOT NULL UNIQUE,        -- 'R-88-03'
    chart_type      text NOT NULL,
    title           text NOT NULL,
    subtitle        text,
    note            text,
    svg             text,                        -- see §5
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (report_id, serial)
);
```

### Serials and labels

`conversations.serial`, `analyses.serial` and `reports.serial` are identity columns:
atomic across app restarts and across tasks behind a load balancer, which an
application-held counter would not be. Only `figures.serial` is per-parent, and a
report is written in one transaction, so it is a plain `row_number()` at insert.

Labels: a figure is `R-<report.serial>-<figure.serial>` (`R-88-03`), an analysis is
`A-<analysis.serial>` (`A-1042`). Both are addresses users can quote, which is the
whole job — the analysis label matters because feature 2 asks people to pick
analyses, and they need something to pick *by*. Conversations keep a serial for the
same reason, so a replayed conversation has a name.

## 4. The three features, in SQL

**1 — Refresh a prior report with new data.** Take its runs, re-execute each
analysis, publish a new report pointing back at the old one. Nothing is overwritten.

```sql
-- the analyses behind report 88, in order, with their current bindings
SELECT a.id, a.serial, a.sql_template, rc.position,
       jsonb_object_agg(ap.name, jsonb_build_object('value', ap.value,
                                                    'expression', ap.expression)) AS params
  FROM reports r
  JOIN report_contents rc   ON rc.report_id = r.id
  JOIN analysis_runs  ar    ON ar.id = rc.analysis_run_id
  JOIN analyses       a     ON a.id = ar.analysis_id
  LEFT JOIN analysis_parameters ap ON ap.analysis_id = a.id
 WHERE r.serial = 88
 GROUP BY a.id, a.serial, a.sql_template, rc.position
 ORDER BY rc.position;
```

A relative `expression` ("last 12 months") re-resolves against today; an absolute
range does not move unless the user asks. That distinction is the whole reason
`analysis_parameters` keeps both `value` and `expression` — a refresh cannot tell
what the user meant from `2025-09-01` alone.

`result_digest` then answers the question a refresh actually raises: *did anything
change?* Comparing digests across two runs flags the analyses worth re-reading,
without storing a single row of PHI.

**2 — Build a report from an arbitrary set of analyses.** `report_contents` already
allows it: run each chosen analysis afresh, then publish with
`origin_conversation_id` null.

```sql
-- what a user can pick from
SELECT a.serial, a.title, a.created_by, max(ar.ran_at) AS last_run
  FROM analyses a LEFT JOIN analysis_runs ar ON ar.analysis_id = a.id
 WHERE a.archived_at IS NULL AND a.created_by = $1
 GROUP BY a.serial, a.title, a.created_by ORDER BY last_run DESC NULLS LAST;
```

**3 — Clone an analysis with different filters.** Copy the specification, rebind the
parameters, record the parent.

```sql
WITH clone AS (
  INSERT INTO analyses (origin_conversation_id, derived_from_id, title, subtitle,
                        note_template, sql_template, chart_type, chart_spec, created_by)
  SELECT $2, id, $3, subtitle, note_template, sql_template, chart_type, chart_spec, $4
    FROM analyses WHERE serial = $1
  RETURNING id
)
INSERT INTO analysis_parameters (analysis_id, name, kind, value, expression, label)
SELECT c.id, p.name, p.kind,
       COALESCE($5::jsonb -> p.name, p.value),        -- rebind what was overridden
       CASE WHEN $5::jsonb ? p.name THEN NULL ELSE p.expression END,
       p.label
  FROM clone c, analyses a, analysis_parameters p
 WHERE a.serial = $1 AND p.analysis_id = a.id;
```

The SQL text is untouched — only the bindings differ — so "the same analysis for a
different patient population" is provably the same analysis.

### And the questions the audit trail answers

```sql
-- published numbers resting on a join the agent never verified
SELECT r.serial, r.title, aj.left_ref, aj.right_ref
  FROM reports r
  JOIN report_contents rc ON rc.report_id = r.id
  JOIN analysis_runs ar   ON ar.id = rc.analysis_run_id
  JOIN analysis_joins aj  ON aj.analysis_id = ar.analysis_id
 WHERE NOT aj.verified;

-- reports whose numbers came from a relation that was stale at the time
SELECT r.serial, r.title, ar.freshness
  FROM reports r
  JOIN report_contents rc ON rc.report_id = r.id
  JOIN analysis_runs ar   ON ar.id = rc.analysis_run_id
 WHERE ar.freshness::text ILIKE '%STALE%';

-- resolve a figure someone quoted in an email
SELECT r.title, r.storage_key, f.title
  FROM figures f JOIN reports r ON r.id = f.report_id
 WHERE f.label = 'R-88-03';
```

## 5. Replaying a conversation

The typed tables above answer questions. They cannot reconstruct a conversation: they
hold outcomes, not the exchange. Replaying one — every message and every agent step —
needs the event stream itself.

**Store it in the shape the UI already consumes.** `AgentSession.send()` yields small
JSON events and `static/index.html` switches on `ev.type` to render them. If those
events are persisted verbatim, replay is the same renderer fed from Postgres instead
of a WebSocket: no second implementation to drift.

```sql
-- One user prompt and the agent's response to it.
CREATE TABLE turns (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq             integer NOT NULL,
    prompt          text NOT NULL,
    started_at      timestamptz NOT NULL DEFAULT now(),
    ended_at        timestamptz,
    duration_ms     integer,
    -- ResultMessage.total_cost_usd is a running conversation total, so this is a
    -- snapshot after the turn, not the turn's own cost. Subtract to get the delta.
    cost_usd_running numeric(10,4),
    is_error        boolean NOT NULL DEFAULT false,
    UNIQUE (conversation_id, seq)
);

-- Append-only. `payload` is exactly what went down the WebSocket.
CREATE TABLE events (
    id              bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    turn_id         uuid REFERENCES turns(id) ON DELETE CASCADE,
    seq             integer NOT NULL,          -- order within the turn
    at              timestamptz NOT NULL DEFAULT now(),
    type            text NOT NULL,             -- 'user'|'assistant_text'|'tool_use'|'tool_result'|'report'|'result'|'error'
    -- Joins a replayed step to its audit row, so a reader can go from "it ran a
    -- query here" to the run that produced the numbers.
    tool_use_id     text,
    payload         jsonb NOT NULL,
    UNIQUE (turn_id, seq)
);
CREATE INDEX ON events (conversation_id, id);
CREATE INDEX ON events (tool_use_id) WHERE tool_use_id IS NOT NULL;

ALTER TABLE analysis_runs ADD COLUMN tool_use_id text;   -- links back to the step
```

Replay is then one query, and the existing front-end switch handles the rest:

```sql
SELECT type, payload FROM events
 WHERE conversation_id = $1 ORDER BY id;
```

### What not to store

**`text_delta`.** They are streaming fragments; `assistant_text` carries the complete
text and `agent.py` already calls it authoritative. Persisting deltas multiplies the
row count for nothing — a replay that wants the typing effect can re-chunk the final
text client-side.

**`hello`, `turn_start`, `turn_end`.** Transport control, not content. A replayer
synthesises them from `turns`.

**The full tool result.** Store the 240-character `preview` the UI shows and the full
tool *input*; never the result body. See below — even the preview is not innocent.

### This makes the transcript a PHI store

`tool_use.summary` for `run_sql` is the SQL, and `tool_result.preview` is the first
240 characters of the result — which for `run_sql` is
`{"row_count": N, "rows": [{…` , i.e. real rows. A query against
`gold.bpa_comments_v2` puts clinician free text into the preview, and therefore into
`events.payload`.

So an `events` table is not a debug log with a different retention story from the
reports; it holds the same class of data. Three options, and the choice is a policy
one:

1. **Store previews as they are** — replay is faithful, and `events` inherits the
   PDFs' handling and retention rules.
2. **Redact on write** — keep `row_count` and the column names, drop the values.
   Replay shows "returned 500 rows" where the live UI showed data. Cheaper to hold,
   and still enough to follow what the agent did.
3. **Redact on read** — store fully, mask unless the viewer is the original
   requester. Most faithful, most machinery.

Option 2 is the one I would default to: the value of replay is seeing *what the agent
did*, and a row preview is rarely the part that matters.

## 6. Decisions that are not mine to make

**Query results are deliberately not stored.** `queries` keeps the SQL, timing and
row count, never the rows. Results from this warehouse include PHI — `bpa_comments_v2`
alone is 1.29M rows of clinician free text. Storing them would create a second PHI
store with its own retention, access-control and breach surface, to enable a
re-render that the archived PDF already provides.
decision: do not store the query results

**`analysis_runs.result_digest` is the compromise that makes refresh useful without
storing rows.** A hash of the result set tells you whether a refresh changed
anything; it cannot reconstruct the data. Choose the digest so it cannot be used as
a membership oracle for small result sets — salt it per deployment.

**`reports.body_markdown` is the same question in a weaker form.** It holds the
aggregates that went into the PDF, and small-cell aggregates can be identifying. It
buys the ability to re-render a report after a template change. The PDF is already
retained, so this is a convenience, not a recovery mechanism — I would leave the
column but ship it unpopulated until someone has decided the retention period.
decision: ok

**`figures.svg` is the milder version again**: a rendered chart contains the plotted
values. Same trade, lower volume.
decision: ok

**Retention and deletion are unspecified here.** `ON DELETE CASCADE` from
`conversations` means deleting a conversation destroys its reports' metadata while
the PDFs survive in object storage — probably the wrong way round. Decide the
retention policy first, then set the cascades to match it.
decision: do not worry about this 

---

## 7. What this does not cover

**The agent cannot produce any of this yet, and that is the critical path.** Its only
structured output is `publish_report(title, subtitle, body_markdown)` — prose with a
SQL appendix. A specification with named parameters cannot be recovered from that:
parsing free SQL to decide which literal is "the patient population" and which is
incidental is guesswork, and it fails silently.

So features 1 and 3 need a `record_analysis` tool the agent calls as it works,
declaring the template, the parameters (with their intent, not just their values),
the relations read and the chart spec. Feature 2 needs only what `record_analysis`
already produces. **Build that tool first** — the schema is inert without it, and its
shape is what decides whether these columns hold anything real.

A useful consequence: the same tool is what lets a chart be rendered from data by
code rather than drawn by the model, which is the standing chart problem too.

**Re-execution semantics are unspecified.** Whether a refresh re-resolves relative
date expressions, or shifts absolute windows forward by their own length, or asks —
that is a product decision. `analysis_parameters` records enough to implement any of
them; it does not choose.

**Migrations.** No tool chosen; the app has no database dependency today.

**Writing the event stream.** `AgentSession.send()` is an async generator consumed by
the WebSocket handler; persisting from inside it couples the agent loop to the
database, and a write failure there would break a live conversation. A second
consumer — or a queue the handler feeds — keeps replay from being able to take the
app down. Unresolved.

**Replaying a conversation is not resuming it.** The schema reconstructs the
transcript; it does not restore the `ClaudeSDKClient` that produced it. Continuing a
past conversation is a different feature, and the SDK's own `resume` and
`session_id` are the mechanism for it — `conversations.sdk_session_id` is already
recorded with that in mind.

**Concurrency on refresh.** Re-running twenty analyses against Snowflake is twenty
queries with no user waiting on them. That wants a job queue, not a request handler,
and nothing here models job state.
