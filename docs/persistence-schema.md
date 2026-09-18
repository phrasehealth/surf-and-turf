# Persistence schema

**Status:** proposed
**Scope:** what the report agent stores in Postgres, and why
**Prerequisite:** there is no database today. `SessionManager.sessions` is an
in-memory dict, and the only durable artefacts are the PDFs in `data/reports/`
(or S3). This introduces a new, **required** dependency: the app does not start
without it.

**Standing assumption:** the system is being changed so that no PHI reaches it. This
document is written for that world — transcripts are stored whole, result previews
are kept, and there is no redaction layer. Until that change actually lands, a
deployment holding real query output inherits the handling rules of the PDFs, and
§7's note on write-time redaction is the interim measure. Local development against
the current warehouse is the case to watch: `run_sql` previews carry real rows today.

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
    -- Correcting an analysis makes a new version rather than editing the old one:
    -- a published report points at a run, and that run's specification must stay
    -- exactly as it was. `lineage_id` is stable across versions, so `A-1042` names
    -- the family and `A-1042.2` names one member.
    lineage_id          uuid NOT NULL,
    version             integer NOT NULL DEFAULT 1,
    supersedes_id       uuid REFERENCES analyses(id) ON DELETE SET NULL,
    -- Feature 3: a clone starts a NEW lineage and records where it came from.
    -- Distinct from supersedes_id: a correction continues a lineage, a clone forks.
    derived_from_id     uuid REFERENCES analyses(id) ON DELETE SET NULL,
    title               text NOT NULL,
    subtitle            text,
    note_template       text,        -- footnote text; parameters interpolated at run time
    chart_type          text,        -- 'hbar' | 'vbar' | 'stacked' | 'stat_tiles' | NULL for table-only
    chart_spec          jsonb,       -- which result columns map to which channel
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text,
    archived_at         timestamptz, -- hidden from pickers; never deleted, runs reference it
    UNIQUE (lineage_id, version)
);
CREATE INDEX ON analyses (created_by, created_at DESC);
CREATE INDEX ON analyses (derived_from_id);
CREATE INDEX ON analyses (lineage_id, version DESC);

-- An analysis may need several queries -- "the top five departments", then monthly
-- counts for those five -- under one title and one footnote. Exactly one is the
-- primary: it is what the table shows and what the chart is drawn from.
CREATE TABLE analysis_queries (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_id  uuid NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    seq          integer NOT NULL,
    purpose      text,                             -- 'cohort', 'ranking', 'series' …
    is_primary   boolean NOT NULL DEFAULT false,
    -- The reusable form, with :named parameters. It need not reproduce the executed
    -- SQL byte for byte -- see `template_verified` on the run.
    sql_template text NOT NULL,
    UNIQUE (analysis_id, seq)
);
CREATE UNIQUE INDEX ON analysis_queries (analysis_id) WHERE is_primary;

-- The swappable parts. `expression` is the intent, `value` the current binding:
-- a refresh re-resolves "last 12 months", an absolute range is left alone.
CREATE TABLE analysis_parameters (
    analysis_id  uuid NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    name         text NOT NULL,                  -- ':date_from', ':dx_codes'
    -- WHAT the values are. Drives the picker: a diagnosis chooser is not a date
    -- chooser. Free text rather than an enum because the list will grow, and a
    -- migration per new domain is friction for no safety.
    kind         text NOT NULL,                  -- see §3.1
    -- HOW it is compared. This is the rule for what counts as a parameter at all
    -- (§3.1), so recording it makes that judgement auditable rather than implicit.
    operator     text NOT NULL,                  -- 'in'|'between'|'gt'|'gte'|'lt'|'lte'|'eq'
    value        jsonb NOT NULL,                 -- the binding
    -- What the user asked for, in their words. Not used by refresh (see §4) — it is
    -- for showing a parameter in a picker and for making a clone's diff legible.
    expression   text,                           -- 'ICD-10 D66*', 'August 2026'
    label        text,                           -- what to call it in the UI: "Patient population"
    PRIMARY KEY (analysis_id, name),
    CONSTRAINT analysis_parameters_operator_check
        CHECK (operator IN ('in', 'between', 'gt', 'gte', 'lt', 'lte', 'eq'))
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
    -- 'adopted'  : bound to results `run_sql` had already produced (the normal path)
    -- 'executed' : the server ran the templates itself, e.g. a refresh
    origin         text NOT NULL DEFAULT 'adopted',
    bound_params   jsonb NOT NULL,               -- snapshot: what the params were THIS time
    database_name  text NOT NULL,
    role_name      text,
    error          text,                         -- set when a query in the run failed
    -- Provenance of the schema knowledge behind the numbers.
    qcp_built_at   timestamptz,
    qcp_conformance text,
    freshness      jsonb,                        -- {'gold.alert_events': 'fresh 2026-09-15', …}
    resolved_note  text                          -- the footnote as rendered for this run
);
CREATE INDEX ON analysis_runs (analysis_id, ran_at DESC);

-- One row per query per run. The executed SQL lives here, not on the specification:
-- an adopted run records what `run_sql` actually sent, which may differ slightly from
-- the template the agent declared.
CREATE TABLE analysis_run_queries (
    run_id            uuid NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    analysis_query_id uuid NOT NULL REFERENCES analysis_queries(id) ON DELETE RESTRICT,
    -- Links an adopted row back to the step in the transcript that produced it.
    tool_use_id       text,
    resolved_sql      text NOT NULL,             -- as executed
    -- FALSE when binding the template did not reproduce `resolved_sql`. Not an error:
    -- templates are allowed to approximate (§7). It is recorded so a refresh can say
    -- which analyses reproduce exactly and which only roughly.
    template_verified boolean,
    duration_ms       integer,
    row_count         integer,
    error             text,
    -- The window this query actually covered. A relative predicate moves it without
    -- the specification changing, so it belongs to the run, not the analysis.
    observed_date_from date,
    observed_date_to   date,
    -- Lets a refresh answer "did anything change?" without keeping the rows.
    result_digest     text,
    PRIMARY KEY (run_id, analysis_query_id)
);
CREATE INDEX ON analysis_run_queries (tool_use_id) WHERE tool_use_id IS NOT NULL;

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

### 3.1 What counts as a parameter

The test is the **shape of the comparison**, not the column. A parameter is anything
filtered by set membership or by range:

| shape | parameter? | example |
|---|---|---|
| `x IN (…)` | yes | `dx_code IN ('D66', 'D66.0')` |
| `x BETWEEN a AND b` | yes | `contact_date BETWEEN :from AND :to` |
| `x > / >= / < / <= v` | yes | `activations >= :threshold` |
| `x = v` | usually not | `is_active_yn = 'Y'` |

An equality is *usually* structure — part of what the analysis means rather than a
choice someone might change. Not always: `master_type = 'LGL'` is a cohort selector
wearing an equals sign. So `eq` is permitted and is the one case that needs judgement;
the other operators are parameters by construction.

That rule is the useful one because the agent can apply it from the SQL it just wrote,
with no taxonomy to consult. Set membership and ranges are exactly the filters someone
later wants to swap; a scalar equality usually is not.

**`kind` values**, which say what a picker should show:

`date_range` · `diagnosis` · `medication` · `orderset` · `procedure` · `alert` ·
`panel` · `flowsheet_row` · `other`

Deliberately not a database enum: the list will grow with the warehouse, and a
migration per new domain buys nothing. `operator` *is* constrained, because that set
is SQL's and does not move.

**Why both columns.** `kind` drives the interface, `operator` drives the binding — a
`diagnosis` bound with `in` needs a multi-select and a list, the same `diagnosis`
bound with `eq` needs one value. Storing only `kind` would leave the clone code
guessing at arity.

### 3.2 Figure naming

A figure is captioned by **its position in the report it appears in** — `Figure 1`,
`Figure 2`. Nothing else.

The earlier `R-88-03` embedded the report serial, and that serial does not exist until
publish. It forced the body to carry placeholders resolved at render, and asked the
agent to cite an identifier it could not yet know. Removing the report serial removes
that problem rather than solving it.

Durable identity does not need to live in the caption, because it already exists
elsewhere: a figure is the chart of an analysis, and the analysis has a serial
(`A-1042.1`) that `record_analysis` already returns. So the caption is for the reader
and the analysis label is for anyone who has to find the thing again — "Figure 2 in
the Stroke Order Set report" for a person, `A-1042.1` for a query.

Two consequences worth stating:

**The agent never writes a figure number.** The server numbers figures when it
renders, from the order of `analyses` passed to `publish_report`. An agent that wants
to cross-reference writes the analysis label as a placeholder (`{{figure:A-1042.1}}`)
and the renderer substitutes `Figure 2` — the same substitution the chart SVG needs,
which is why it is worth building once rather than twice.

**`figures.label` is not unique.** Every report has a `Figure 1`.
`UNIQUE (report_id, serial)` is the constraint that actually has to hold.

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
SELECT a.id, a.serial, a.version, rc.position,
       jsonb_agg(jsonb_build_object('seq', q.seq, 'primary', q.is_primary,
                                    'sql', q.sql_template) ORDER BY q.seq) AS queries,
       (SELECT jsonb_object_agg(ap.name, jsonb_build_object('value', ap.value,
                                                            'expression', ap.expression))
          FROM analysis_parameters ap WHERE ap.analysis_id = a.id) AS params
  FROM reports r
  JOIN report_contents rc ON rc.report_id = r.id
  JOIN analysis_runs  ar  ON ar.id = rc.analysis_run_id
  JOIN analyses       a   ON a.id = ar.analysis_id
  JOIN analysis_queries q ON q.analysis_id = a.id
 WHERE r.serial = 88
 GROUP BY a.id, a.serial, a.version, rc.position
 ORDER BY rc.position;
```

**Refresh has exactly one behaviour: re-execute the specification unchanged.** No
parameter is rewritten and the user is not asked anything.

That works because relativity lives in the SQL, not in a binding. An analysis that
means "the last twelve months" is written
`WHERE contact_date >= DATEADD(month, -12, CURRENT_DATE)`, so re-running it covers a
later window by construction. An analysis pinned to August 2026 binds those dates as
parameters, and re-running it correctly returns the same window — a refresh should
not silently move a period the user chose.

So the two cases need no branch, no configuration and no prompt, and the
`record_analysis` tool is where the distinction gets made: the agent writes a
relative predicate or binds an absolute one, according to what the user asked for.

Because the window can move without the specification changing, each run records the
range each of its queries actually covered — `observed_date_from` /
`observed_date_to` on `analysis_run_queries` — so two runs of the same analysis can be
compared without re-reading their SQL. That is also what the
footnote needs: `CLAUDE.md` requires an analysis to state its date range, and for a
relative predicate that sentence is only true of the run that produced it, which is
why `resolved_note` is stored per run rather than on the specification.

`result_digest` then answers the question a refresh actually raises: *did anything
change?* Comparing digests across two runs flags the analyses worth re-reading,
without keeping a copy of the data to diff.

**2 — Build a report from an arbitrary set of analyses.** `report_contents` already
allows it: run each chosen analysis afresh, then publish with
`origin_conversation_id` null.

```sql
-- what a user can pick from. Most recorded analyses were never published, so the
-- picker needs a relevance signal; whether one ever reached a report is a good one
-- and costs no extra column.
SELECT a.serial, a.version, a.title, a.created_by,
       max(ar.ran_at) AS last_run,
       EXISTS (SELECT 1 FROM report_contents rc
                 JOIN analysis_runs r2 ON r2.id = rc.analysis_run_id
                WHERE r2.analysis_id = a.id) AS was_published
  FROM analyses a LEFT JOIN analysis_runs ar ON ar.analysis_id = a.id
 WHERE a.archived_at IS NULL AND a.created_by = $1
   -- only the current version of each lineage
   AND NOT EXISTS (SELECT 1 FROM analyses newer
                    WHERE newer.lineage_id = a.lineage_id AND newer.version > a.version)
 GROUP BY a.id, a.serial, a.version, a.title, a.created_by
 ORDER BY was_published DESC, last_run DESC NULLS LAST;
```

**3 — Clone an analysis with different filters.** Copy the specification, rebind the
parameters, record the parent.

```sql
WITH clone AS (
  -- A clone forks: new lineage, version 1, `derived_from_id` recording the parent.
  INSERT INTO analyses (lineage_id, version, origin_conversation_id, derived_from_id,
                        title, subtitle, note_template, chart_type, chart_spec, created_by)
  SELECT gen_random_uuid(), 1, $2, id, $3, subtitle, note_template,
         chart_type, chart_spec, $4
    FROM analyses WHERE serial = $1
  RETURNING id
), copied_queries AS (
  INSERT INTO analysis_queries (analysis_id, seq, purpose, is_primary, sql_template)
  SELECT c.id, q.seq, q.purpose, q.is_primary, q.sql_template
    FROM clone c, analyses a, analysis_queries q
   WHERE a.serial = $1 AND q.analysis_id = a.id
)
INSERT INTO analysis_parameters (analysis_id, name, kind, value, expression, label)
SELECT c.id, p.name, p.kind,
       COALESCE($5::jsonb -> p.name, p.value),        -- rebind what was overridden
       CASE WHEN $5::jsonb ? p.name THEN NULL ELSE p.expression END,
       p.label
  FROM clone c, analyses a, analysis_parameters p
 WHERE a.serial = $1 AND p.analysis_id = a.id;
```

The SQL templates are copied verbatim — only the bindings differ — so "the same
analysis for a different patient population" is recognisably the same analysis.

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

-- which analyses read a relation, without running anything: the question asked
-- when a model is about to change or has gone stale
SELECT a.serial, a.title, a.created_by
  FROM analyses a
  JOIN analysis_relations ar ON ar.analysis_id = a.id
 WHERE ar.schema_name = 'gold' AND ar.relation_name = 'flowsheet_catalog'
   AND a.archived_at IS NULL;

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

### What a stored event actually contains

`tool_use.summary` for `run_sql` is the SQL, and `tool_result.preview` is the first
240 characters of the result — for `run_sql`, `{"row_count": N, "rows": [{…`, i.e.
real rows. Under the no-PHI assumption that is fine and worth keeping: a replay that
shows what came back is more useful than one that shows only a row count.

It is also the single place that assumption is load-bearing. If a deployment ever
queries data that is not safe to retain, `events.payload` is where it lands, and the
fix is in `_preview` — redact at the point of creation so the live UI and the stored
transcript agree, rather than bolting a filter onto the writer.

## 6. Resuming a conversation after a restart

Replay reconstructs the transcript for a human. Resuming needs the *model's* history
back, in the CLI's own format, and that is a different problem with a purpose-built
answer in the SDK.

**Today it cannot work.** The Claude Code subprocess writes transcripts under
`CLAUDE_CONFIG_DIR`, which the Dockerfile sets to `/home/agent/.claude`. There is no
volume for it in `infra/ecs-task-definition.json`, so a task replacement destroys
every session. `ClaudeAgentOptions.resume` would then find nothing.

**`ClaudeAgentOptions.session_store` is the mechanism.** Its contract:

> Mirror session transcripts to an external store. When set, every transcript line
> written locally is also passed to `session_store.append()`, and `resume` can
> materialize from the store when the local file is absent.

Only `append()` and `load()` are required, and the failure semantics are the ones
this app wants: `append` is called **after** the local write has already succeeded,
batches at roughly 100ms, retries three times, and on failure surfaces a
`MirrorErrorMessage` while the subprocess continues unaffected. Persistence cannot
take down a live conversation.

```sql
-- The SDK's own transcript, mirrored. Not the same thing as `events`: this is the
-- model's history in the CLI's format, written for `resume` to read back.
CREATE TABLE sdk_transcript_entries (
    session_id  text NOT NULL,
    project_key text NOT NULL,           -- from project_key_for_directory(cwd)
    -- Most entries carry a stable uuid the SDK asks adapters to treat as an
    -- idempotency key. Entries without one (titles, tags, mode markers) append.
    entry_uuid  uuid,
    seq         bigint NOT NULL GENERATED ALWAYS AS IDENTITY,
    entry       jsonb NOT NULL,
    written_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ON sdk_transcript_entries (session_id, entry_uuid)
    WHERE entry_uuid IS NOT NULL;
CREATE INDEX ON sdk_transcript_entries (session_id, seq);
```

`load()` returns the entries for a session in `seq` order. Deep equality is enough —
the SDK never byte-compares, so `jsonb` key reordering is fine, which is stated
explicitly in the protocol.

Then resuming is:

```python
ClaudeAgentOptions(
    resume=conversation.sdk_session_id,   # already on `conversations`
    session_store=PostgresSessionStore(pool),
    ...
)
```

and `CLAUDE_CONFIG_DIR` should move to `/tmp`, which the protocol suggests: the local
copy becomes an explicitly ephemeral scratch file rather than something that looks
durable and is not.

### Why both this and `events`

They answer different questions and neither derives safely from the other.
`sdk_transcript_entries` is the CLI's internal format, whose shape is not a contract
we control — building the replay UI on it would break on a CLI upgrade. `events` is
our own wire format, which the front end already renders. The cost is that the conversation is stored
twice, in two formats — acceptable, but worth knowing before someone wonders why a
conversation appears in both.

## 7. Recommendations on the open decisions

Each of these is reversible in principle and painful in practice, so here is what I
would pick and why.

**Cascades — stop cascading from `conversations`.** As drafted, deleting a
conversation destroys its reports' metadata while the PDFs live on in object storage:
the artefact survives and the record of who produced it does not, which is the wrong
way round for anything auditable. Change `reports` and `analysis_runs` to
`ON DELETE RESTRICT` and never hard-delete a conversation — add
`conversations.deleted_at` and filter on it.

**Event redaction — none, per the standing assumption.** Store previews whole. The
one thing to keep in view: if that assumption stops holding, redact inside `_preview`
rather than in the writer, so the live UI and the stored transcript never disagree
about what was returned.

**`result_digest` — HMAC-SHA256 with a per-deployment key** over the canonicalised
result. The key costs nothing and keeps the digest from being a membership oracle for
small result sets, which is cheap insurance whatever the data turns out to be.

**Driver and migrations — `asyncpg` with SQLAlchemy Core, and Alembic.** The app is
async FastAPI throughout; a sync driver would need `to_thread` for every write, which
is the pattern already causing the unbounded-query problem in `snowflake_sql.py`.
Core rather than the ORM because this schema is written against, not navigated, and
the interesting queries in §4 are ones you want to write in SQL. Alembic because the
alternative is hand-ordered `.sql` files and a convention nobody follows after month
three.

**Where writes happen — a queue and a background writer.** `AgentSession.send()` must
not await Postgres: a write failure there breaks a live conversation over data that
is only wanted afterwards. The SDK's own `SessionStore` contract is the model to
copy — append after the fact, batch, retry three times, surface the error, let the
conversation continue. Use the same shape for `events`: `send()` puts the event on an
`asyncio.Queue` and returns; a task drains it. Accept that a hard crash loses the
tail of the last turn, because the alternative is a database outage taking the
product down.

**Resume — implement `SessionStore` (§6), and move `CLAUDE_CONFIG_DIR` to `/tmp`.**
Storing `sdk_session_id` is necessary and not sufficient; without the mirrored
transcript, `resume` has nothing to load once the task is replaced.

**`record_analysis` — design the tool before the DDL.** Suggested shape, since it is
what fixes half these column types:

```python
record_analysis(
    title, subtitle, note_template,
    queries,          # [{tool_use_id, sql_template, purpose, primary: bool}] — in order
    parameters,       # [{name, kind, operator, value, expression, label}] — §3.1
    relations,        # ["gold.order_events_expanded", …]
    joins,            # [{left, right, verified, match_pct}]
    chart_type=None, chart_spec=None,
    supersedes=None,  # 'A-1042' when correcting: makes version 2, not a new analysis
) -> analysis_label   # 'A-1042.1', which the agent then cites in the body
```

**Adopt, do not execute.** Each entry in `queries` names the `tool_use_id` of a
`run_sql` the agent has already made, so the server binds the specification to
results it already holds rather than running everything twice. That needs a small
per-conversation cache of recent `run_sql` results — bounded anyway, since `run_sql`
caps at `SQL_ROW_LIMIT` rows. If a `tool_use_id` has fallen out of the cache, fall
back to executing the template; a refresh takes that path by definition, which is
what `analysis_runs.origin` records.

**The template may approximate.** Binding the parameters need not reproduce the
executed SQL byte for byte. The server compares anyway and stores the result in
`analysis_run_queries.template_verified` — not to reject the call, but so that a
refresh can say which analyses reproduce exactly and which only roughly. Silent
approximation is the thing to avoid, not approximation.

**Corrections make a version.** `supersedes='A-1042'` writes a new `analyses` row
sharing the lineage with `version = 2`. The old row is untouched, because a published
report points at a run whose specification must stay exactly as it was. A clone
(feature 3) is the other relationship: new lineage, `derived_from_id` set.

It returns the label, so the agent writes it into the report body and the
figure/analysis linkage is established by the server rather than asserted by the
model — the same principle as the download URL and the figure SVG.

**Recording is independent of publishing.** Plenty of conversations end with the user
having got what they needed and no PDF produced, and plenty of others record more than
they publish — a cohort that turned out wrong, a figure the user did not want, a
question answered on the way to a better one. Those analyses are still worth keeping:
they are what feature 2 later picks from, and an abandoned attempt is often the thing
someone wants to resume. So the cart is simply the set of recorded analyses on a
conversation, and `publish_report` selects an ordered subset of it:

```python
publish_report(title, subtitle, body_markdown,
               analyses)   # ordered labels: ['A-1042.1', 'A-1043.1']
```

**`publish_report` refuses a body whose analyses were not recorded**, and the refusal
is written to be actionable rather than merely correct — the agent reads it and fixes
itself, the way a Snowflake error already teaches it a column name:

> Rejected: `analyses` is empty, but `body_markdown` has 3 `##` sections. Every
> analysis in a report must be recorded first. For each one call
> `record_analysis(title, subtitle, note_template, queries=[{tool_use_id, sql_template,
> purpose, primary}], parameters, relations, joins, chart_type, chart_spec)` — pass
> the `tool_use_id` of the `run_sql` calls you already made, so nothing is re-run. It
> returns a label like `A-1042.1`. Then call `publish_report` again with those labels
> in `analyses`, in the order they appear in the body.

The check is: `analyses` is non-empty, every label resolves to an analysis recorded in
this conversation, and the count matches the number of `##` sections in the body. The
last of those is a heuristic and should warn rather than reject — a report may
legitimately carry a section that is not an analysis.

**Where this is heading.** Once an analysis carries its own title, subtitle, footnote
and chart, the body no longer needs to contain them: the server can render the report
from the ordered list of analyses, and the agent supplies only the connecting prose.
That is how the chart and footnote standards stop being instructions in `CLAUDE.md`
and become properties of the output. Not a step to take at the same time as this one,
but the reason to keep `body_markdown` and `analyses` as separate arguments rather
than parsing one out of the other.

## 8. Implementation plan

Local Postgres only. Nothing here provisions cloud infrastructure; the point is a
working, required persistence layer a contributor can stand up in five minutes.

### 8.1 Stage one — the walking skeleton

Pay the plumbing cost once, on one table, before writing eleven against assumptions
that may be wrong. Done when a conversation row is written and read back end to end,
in the app and in the tests.

**Dependencies.** `requirements.txt` gains:

```
sqlalchemy[asyncio]>=2.0
asyncpg>=0.29
alembic>=1.13
```

Core rather than the ORM: this schema is written against, not navigated, and the
queries in §4 are ones you want to read as SQL.

**A database to talk to.** `docker-compose.yml` gains a service, and the app service
gains a dependency on it:

```yaml
  postgres:
    image: postgres:17-alpine
    environment:
      POSTGRES_DB: report_agent
      POSTGRES_USER: report_agent
      POSTGRES_PASSWORD: report_agent     # local only; prod reads a secret
    ports: ["5432:5432"]
    volumes: [report_agent_pg:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U report_agent"]
      interval: 5s
      timeout: 3s
      retries: 10

volumes:
  report_agent_pg:
```

and on `report-agent`:

```yaml
    depends_on:
      postgres: { condition: service_healthy }
    environment:
      DATABASE_URL: postgresql+asyncpg://report_agent:report_agent@postgres:5432/report_agent
```

**Configuration.** `app/config.py` gains `database_url` and `db_pool_size`, and
`.env.example` documents them. Because persistence is required, an unset
`DATABASE_URL` is a startup failure with a message naming the compose command — not
a silent fallback to in-memory, which would look like it worked.

**Lifecycle.** The engine and pool are created in the existing `lifespan` in
`app/main.py`, beside the session reaper, and disposed in its `finally`. `/healthz`
runs `SELECT 1`; a database that is down makes the app unhealthy, which is the
correct signal now that it cannot work without one.

**Migrations.** `alembic init`, with `env.py` pointed at `settings.database_url` and
`sqlalchemy.url` left out of `alembic.ini` so the credential is never committed.
Migrations are run as a deliberate step (`alembic upgrade head`), never on startup —
on startup is convenient until two tasks race the same migration.

**Tests.** They currently need no network and no services, which is worth keeping as
close to true as it can be. A session-scoped fixture creates a uniquely-named
database, runs `alembic upgrade head` against it, and drops it at the end; each test
gets a transaction that is rolled back. `conftest.py` skips the database tests with a
clear message when nothing is listening on 5432, so `pytest` still tells a contributor
what to start rather than erroring obscurely.

**The one table.** `conversations` and its migration, a thin repository module, and
`SessionManager.create()` writing a row. Read it back in `GET /conversations/{id}`.

### 8.2 Stage two — the schema

In dependency order, each with its own migration:

1. `turns` and `events` — they need only what the app already emits, so they can land
   before `record_analysis` exists and immediately make replay possible.
2. The event queue and background writer (§7). `send()` enqueues and returns;
   a task drains. Do this with `events`, not after it, or the coupling gets baked in.
3. `analyses`, `analysis_queries`, `analysis_parameters`, `analysis_relations`,
   `analysis_joins`, `analysis_runs`, `analysis_run_queries` — blocked on the
   `record_analysis` tool. Done — the read-back change this once pointed at is
   optional and stays unbuilt (§9).
4. `reports`, `report_contents`, `figures` — `publish_report` writes them; figures
   need the chart work.
5. `sdk_transcript_entries` and the `SessionStore` adapter, plus moving
   `CLAUDE_CONFIG_DIR` to `/tmp`. **Verify with a real restart**, not a unit test:
   `SessionKey` is a TypedDict, so a store that reads it with `getattr` silently
   mirrors nothing and resume fails only in production.

### 8.3 What this changes elsewhere

- **`README.md`** — "Run locally in 60 seconds (no credentials)" becomes a five-minute
  path that starts `docker compose up -d postgres` and runs `alembic upgrade head`.
  The mock-mode promise survives; the no-dependencies promise does not.
- **`Dockerfile`** — no change; the app reaches Postgres over the network.
- **`infra/ecs-task-definition.json`** — out of scope here, and deliberately so.

### 8.4 Deferred until there is more than one instance

Connection limits and a pooler, read replicas, backups and restore drills, migration
gating in CI, and anything to do with a managed database. All of it is real work; none
of it is needed to build and prove the schema locally.

## 9. Still open

The execution model, granularity, template fidelity, correction semantics, cart
`record_analysis`, its result cache and the publish gate are **built**
(`app/tools/record_analysis.py`, `app/tools/result_cache.py`). `chart_spec` stays
`jsonb` with no enforced schema — the agent supplies the column-to-channel mapping,
and it can be tightened later if `charts.py` wants a stricter contract. What remains:

**Reading back from the cart — possible now, not planned.**

The dependency is gone: `record_analysis` is built and `list_analyses()` already
returns a conversation's cart. This is now a choice rather than a blocked item, and
the choice is to leave it.

What it would change. Today the read-back is recollection — `CLAUDE.md` asks the
agent to list each analysis with its title, what it shows, its visualization and its
filters, and the agent composes that from memory. Nothing checks it against what was
actually recorded, so it can drift: describing three analyses when two were computed,
or naming a chart it does not then produce.

Building it is four small pieces:

1. `CLAUDE.md` instructs the agent to read back from the cart rather than from memory.
2. A `list_analyses()` tool exposes it (the repository function exists; only the MCP
   wrapper is missing).
3. The read-back renders the analyses **proposed for this report** — not the whole
   cart, since most conversations record more than they publish.
4. `publish_report`'s gate tightens: the labels published must be ones the user saw.

Why it is not urgent. The drift it removes is real but narrow, and the publish gate
already prevents the damaging version — a report cannot contain an analysis that was
never recorded, whatever the read-back said. Worth doing when the read-back is
observed to be wrong in practice, rather than on principle.

**Concurrency on refresh.** Re-running twenty analyses is twenty Snowflake queries
with no user waiting. That wants a job queue, not a request handler, and nothing here
models job state. It blocks nothing in §8.
