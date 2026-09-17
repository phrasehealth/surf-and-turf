# Schema context for the report agent: a generated catalog

**Status:** proposed
**Scope:** what context the report agent is given about the source database, and how each kind is produced and kept true
**Measurements:** taken against `snowflake-etl/epic/transforms` @ `target/manifest.json`, 758 models

The agent writes SQL against Snowflake. To do that it has to know which relations
exist, what each one means, and what its columns are — and it has to recognise them
under two names, because users ask in Epic Clarity terms (`ORDER_PROC`) for tables
the warehouse calls something else (`bronze.hv_order_proc`). This document argues that
mounting the dbt repository and letting the agent explore it is the wrong way to
supply that knowledge, and specifies a generated catalog to replace it.

---

## 1. Why not mount the raw transforms repo

The current plan, written into `workspace/CLAUDE.md` and
`workspace/transforms/README.md`, is to add the transforms repo as a git submodule
and tell the agent:

> The `transforms/` directory is the dbt/transform repository. Its models define
> every table you may use. Before writing SQL, look up the model
> (`Glob transforms/**/*.sql`, `Read` the model and its `.yml` schema) so column
> names and grain are right.

Against the real repository, every clause of that instruction fails.

### 1.1 The documentation is not where the instruction sends the agent

**Zero of 759 models have a same-named `.yml` next to them.** Column documentation
lives in 20 directory-level `schema.yml` files:

| file | size |
|---|---|
| `models/phrasehealth/bronze/reference/schema.yml` | 238 KB |
| `models/phrasehealth/bronze/date/schema.yml` | 145 KB |
| `models/phrasehealth/silver/deduped/schema.yml` | 103 KB |
| `models/phrasehealth/gold/content_compass/alerts/schema.yml` | 84 KB |
| …16 more | |

So "Read the model and its `.yml` schema" resolves to: read an 84 KB file (~21k
tokens) to extract the 25 lines describing one model. The agent either burns the
context window or greps and gets a fragment with no idea which model it belongs
to. This is not a tuning problem — the instruction describes a repo layout that
does not exist.

### 1.2 The model files do not name the tables they produce

**All 758 models contain Jinja; only 101 use `ref()`.** Every model body is macro-driven.
What sits on disk is a template, not the SQL that ran, and it contains no
reliable statement of the relation it lands in. An agent that reads
`alert_catalog_v2.sql` cannot come away knowing the table is
`penn.gold.alert_catalog_v2`. It has to guess — which is exactly what
`CLAUDE.md` forbids.

### 1.3 A model's schema is stated nowhere the agent can look

The manifest says every one of the 758 models lives in `gold`. That is false for
591 of them, and the reason is worth following.

Schema is not a property of a model. It is a property of the dbt *invocation*.
`epic/Makefile` and `cerner/Makefile` pair a directory with a target:

```make
dbt run --select models/phrasehealth/bronze/* --target $(TARGET_ORG)_bronze
dbt run --select models/phrasehealth/silver/* --target $(TARGET_ORG)_silver
dbt run --select models/phrasehealth/gold/*   --target $(TARGET_ORG)_gold
```

and `profiles.yml` defines `<org>_<layer>` as `database: <org>, schema: <layer>`.
Both `dbt_project.yml` files say so explicitly: *"schema comes from the TARGET NAME."*
So the layer directory determines the schema — but only because CI pairs them, and
that pairing lives in a Makefile, not in anything dbt exposes.

The committed manifest is one such invocation: `GRAPH_TARGET ?= uvm_gold`. Every
model in it is therefore stamped `database: uvm, schema: gold`, including the 261
bronze and 330 silver models that really land in `bronze` and `silver`. An agent
handed the repo has three sources that disagree — a manifest that says `gold`, a
directory that says `bronze`, and a Makefile that resolves it — and no reason to
prefer the third.

The hazard is concrete. `gold.hv_order_proc` does not exist; the relation is
`bronze.hv_order_proc`. And a query against a plausible-looking wrong schema does
not always fail: `bronze_cross_schema_ref` hardcodes `target_schema='BRONZE'`, so
raw Clarity landing tables are genuinely queryable under names an agent would guess.

### 1.4 Grep has no selectivity at this size

Naive search over 759 models returns far too much:

| search term | models matched |
|---|---|
| `alert` | 98 |
| `order` | 435 |
| `medication` | 68 |
| `encounter` | 45 |

A report question about ordering variation would put the agent in front of 435
candidate files with nothing to rank them by. Grep returns matching lines, not
grain, relation name or meaning — none of what it needs to choose.

### 1.5 The knowledge is re-derived every conversation, differently

Schema exploration through Glob/Grep/Read costs turns and tokens on every single
report, produces a different subset of the truth each time, and is capped by
`AGENT_MAX_TURNS` and `AGENT_MAX_BUDGET_USD`. Two users asking the same question
get answers built on different table choices. Schema knowledge is stable and
shared; paying to rediscover it per conversation is the wrong shape.

### 1.6 The mount drags in 9,728 files

The repo is 9,728 files; `models/` is 4.2 MB of it. The rest — `target/`, `logs/`,
`dags/`, `seeds/`, `macros/`, `terraform/` — is irrelevant to writing a SELECT, and
some of it (profiles, seeds, extract configs) is material we have no reason to put
inside a container that talks to an LLM. `Dockerfile:19` does `COPY workspace ./workspace`,
so all of it ships in the image.

### 1.7 The Clarity vocabulary is in the repo, but not in the graph

Users ask questions in Epic Clarity terms — `ORDER_PROC`, `PAT_ENC_CSN_ID`,
`V_CODING_ALL_DX_PX_LIST_V2` — because that is the vocabulary of the source system
they know. The bronze layer is exactly the Clarity→warehouse crosswalk, and it is
recoverable, but not through any interface the agent has:

- **There are zero dbt `sources` defined.** All 261 bronze models are graph roots
  with no upstream edges. The Clarity→bronze relationship does not exist in
  `depends_on`, so no amount of lineage traversal will find it.
- It survives only as a **header comment** (`-- Epic HV_ORDER_PROC table with
  Snowpipe auto-ingestion`, present on 256 of 261 models) and as the **pre-hook DDL**
  naming the landing table and its columns (present on 261 of 261).

Both are comments and config to a reader, not structure. Recovering the crosswalk
means parsing model source — which is the generator's job, done once, not something
to ask an agent to do live against 261 files.

### 1.8 The fair counter-argument

Raw model SQL **is** the ground truth for business logic. When someone asks "what
exactly counts as an override," the answer is in `alert_override_rate_annual.sql`
and nowhere else. The argument above is not that the SQL is useless — it is that
the SQL is a terrible *entry point*. Keep it, reachable by exact path, as the
escape hatch rather than the front door (§2.6).

---

## 2. The design: five tiers of context

Tiers are separated by **epistemic status** — how a fact is obtained, how it fails,
who maintains it, and how often it has to be refreshed — rather than by how the
agent reads it. That ordering is also roughly the order of importance: tier 1 is
required for any correct query, and each tier after it prevents a narrower class of
wrong answer.

| tier | what it is | source | fails by | refresh | cost |
|---|---|---|---|---|---|
| 1 — **Structure** | tables, columns, types, descriptions, table lineage, Clarity crosswalk | dbt manifest + `INFORMATION_SCHEMA` | going stale on a merge | on transforms merge | free |
| 2 — **Join paths** | how tables actually connect, and on what | 2 declared + empirical value-overlap | suggesting a join that doesn't hold | with the profiling job | one scan pass |
| 3 — **Shape & freshness** | row counts, last write, latest business date | `INFORMATION_SCHEMA` + one `MAX()` per date column | silently lagging reality | nightly | cheap |
| 4 — **Column profiles** | null rates, distinct counts, top-10 values | full column scans | stale value domains | weekly | expensive |
| 5 — **Strategy** | "for referrals, join orders to encounters and filter X" | authored by humans | being wrong, quietly | on review | human time |

Only **tier 1 is standing context** (~10.6k tokens). Tiers 2–5 are read on demand,
keyed by the table names tier 1 supplies. A page in any tier must state which tier
it came from, so the agent can tell a derivation from an assertion and verify
before trusting tier 5 (§2.5).

Note that the tier number ranks *importance and trust*, not build order: tier 2 is
produced by the same profiling job as tiers 3 and 4, but ranks above them because a
wrong join produces a wrong number while a missing row count merely produces a
cautious one.

### 2.1 Tier 1 — Structure

Deterministic, free, and the only tier that is always in context. Three files,
plus the question of where provenance belongs (§2.1.4).

#### 2.1.1 `catalog/index.md`

One line per model, grouped by dbt directory. Measured at **27 KB (~6.8k tokens)**
for the 167-model gold surface, which is small enough to sit in context for every
conversation and be prompt-cached across turns.

**Scope: `gold/` and `global/` only.** Silver models are intermediates and
including all 758 models triples the index to ~21.7k tokens. Bronze is excluded
here *but not excluded from the catalog* — it gets its own entry point in §2.2,
because 102 of the 261 bronze tables have no downstream model at all and are the
only place their Clarity data lives. Omitting them entirely, as an earlier draft of
this design did, would have made them unfindable.

Real generated output:

```markdown
# Table catalog

## gold/content_compass/alerts

- **alert_action_categories** — Static mapping of Epic alert action IDs to action categories (Override or Accept). Action IDs not present in this table are treated as Neutral by cons…
- **alert_burden_index** — Alert Burden Index - measures alert frequency normalized by unique users. Tracks 30-day and annual rolling burden metrics to identify alert fatigue. C…
- **alert_catalog_v2** — Alert catalog ACCESS LAYER — the denormalized "wide" read assembled from dim_alert + the metric facts + the typed satellites. Materialized as a dynami…
- **alert_event_actions** 🔒 — Normalized alert event actions table (one row per action). Avoids arrays for better BI tool compatibility and query performance. Includes date_time fo…
- **alert_events** 🔒 — Alert events with classification and interruptivity logic. Aggregates alert data with complex type determination (EMI/MAR/MED/DIS/LGL). **DAILY REFRES…
- **alert_opportunity_score** — Composite "opportunity" metric fact (DATAENG-1512). Rescales burden / cranky / override / firing volume to 0-100 and combines them into a single ranke…
- **dim_alert** — Conformed alert identity + lifecycle dimension. One row per alert, all 5 types. The single identity spine of the alert star; metric-free by design (th…

## gold/content_compass/ordersets
…
```

The 🔒 marks models carrying the `has_phi` dbt tag (65 models repo-wide). The agent
should prefer an unmarked aggregate when one will answer the question; surfacing
the tag at the point of table selection is the cheapest place to influence that.

Descriptions are truncated to 150 characters. The truncation is deliberate: the
index exists to let the agent *choose*, not to let it write SQL. Choosing is a
recall problem, and 150 characters is enough to discriminate between
`alert_events` and `alert_catalog_v2`.

#### 2.1.2 `catalog/clarity.md`

The bridge from the vocabulary users actually speak to the relations that exist.
One row per bronze model, generated by parsing the `-- Epic <TABLE> table` header
and the pre-hook DDL (§1.7). Measured at **15 KB (~3.8k tokens)** for all 261.

```markdown
# Epic Clarity crosswalk

| Clarity table | relation | gold models downstream |
|---|---|---|
| `ALERT_CRITERIA` | `{bronze}.alert_criteria` | — query directly |
| `ALERT_HISTORY_V2` | `{bronze}.alert_history_v2` | 37 |
| `HV_ORDER_PROC` | `{bronze}.hv_order_proc` | — query directly |
| `V_CODING_ALL_DX_PX_LIST_V2` | `{bronze}.v_coding_all_dx_px_list_v2` | 11 |
```

The bronze model *is* the Clarity table, not a copy of it. Each one's pre-hook
creates the Snowpipe landing table and its body is `select * from BRONZE.<TABLE>`
with `where 1=0` on incremental runs — so when built with a `*_bronze` target it
materializes onto the relation it selects from, which makes it a no-op registration
of the landing table into dbt's graph. Clarity's table name, column names, types and
grain therefore carry through unchanged. The crosswalk is a rename table, not a
mapping that can drift.

The downstream count is the routing signal. A non-zero count means the question is
probably better answered from a modeled gold table, and the agent should read that
model's page before reaching for raw Clarity. **`— query directly` means the bronze
table is the only source** — true for 102 of 261, including `HV_ORDER_PROC`. Without this file those 102 tables are invisible to the agent.

Bronze model pages (§2.3) carry the reverse detail: the Clarity source name, the
full downstream list, and column types recovered from the pre-hook DDL. Those DDL
types are worth calling out — they are **the only column types anywhere in the
manifest**, since `data_type` is null throughout (§3):

```markdown
# hv_order_proc

`{database}.{bronze}.hv_order_proc` · incremental
Clarity source: `HV_ORDER_PROC` (landed by Snowpipe into `BRONZE.HV_ORDER_PROC`)

## Columns

| column | type | description |
|---|---|---|
| `ORDER_PROC_ID` | NUMBER | |
| `DISCR_FREQ_ID` | STRING | |
| `STAND_CNT` | NUMBER | |
| `STND_TP_C` | NUMBER | |
| `extract_date` | DATE | Snowpipe extract partition |
```

#### 2.1.3 `catalog/models/<name>.md`

One file per model, addressed by model name, so the agent reads an exact path with
no search. Generated for **all 758 models** — disk is free and these are only read
on demand. Median page is 694 characters (~173 tokens); the largest is 8.1 KB
(~2k tokens).

Real generated output, abridged in the column table only:

```markdown
# alert_catalog_v2

`{database}.gold.alert_catalog_v2` · dynamic_table

Alert catalog ACCESS LAYER — the denormalized "wide" read assembled from dim_alert
+ the metric facts + the typed satellites. Materialized as a dynamic table
(read-hot for the v3 API); a leaf, so it reads the facts for the high_* flags
without a base/v2 split. What the app catalog/detail pages read.

Grain: `alert_catalog_id`

## Columns

| column | description |
|---|---|
| `alert_catalog_id` | Composite alert key (from dim_alert). |
| `burden` | 30-day burden (alert_burden_index.thirty_day_index). |
| `previous_burden` | Burden in the previous 30-day window (alert_burden_index.previous_thirty_day_index). Compare to burden to see if it's trending up or down. |
| `burden_severity` | Severity classification ('High'/'Medium'/'Low') of the current 30-day burden (alert_burden_index.burden_severity). >=1.0 High, >=0.5 Medium. |
| `thirty_day_pct_override` | Override rate in the current 30-day window (alert_override_rate_annual.thirty_day_pct_override). |
| `composite_score` | Weighted opportunity score, 0-100 (alert_opportunity_score.composite_score). |
| `is_top_opportunity` | composite_score >= 25 (alert_opportunity_score.is_top_opportunity). |
| … | 25 columns total |

## Lineage

Reads: `dim_alert`, `alert_burden_index`, `cranky_alert_index`, `alert_override_rate_annual`, `alert_opportunity_score`, `dim_alert_lgl_config`, `dim_alert_emi`

---
dbt model `models/phrasehealth/gold/content_compass/alerts/alert_catalog_v2.sql`
```

Note what the existing column descriptions already give us for free: provenance
(`alert_burden_index.thirty_day_index`), thresholds (`>=1.0 High, >=0.5 Medium`)
and usage hints (`Compare to burden to see if it's trending up or down`). This is
exactly the material that makes the difference between correct and plausible SQL,
and today it is buried in an 84 KB `schema.yml` the agent will never usefully read.

Lineage is emitted in both directions. Downstream edges are computed by inverting
`depends_on` across the manifest, which is what makes provenance questions
tractable:

```markdown
## Lineage

Reads: `cl_lgl_ovrtme_sing_deduped`, `cl_lgl_noadd_sing_deduped`, `record_status`, `lgl_record_type`, `importance_lvl`, `alert_v2_deduped`, `alert_history_v2_deduped`, `med_interaction_deduped`, `alt_type`, `med_alert_type`
Read by: `alert_burden_index`, `alert_catalog_v2`, `alert_events_expanded`, `alert_opportunity_score`, `alert_override_rate_annual`, `alert_search_feeder`, `alerts_by_day_last_30_days`, `alerts_last_90_day_analytics`, `cranky_alert_index`, `interruptive_alerts_by_date_raw`, `new_alert_firing_trends`, `past_year_summary`
```

Where dbt documentation is thin, the page is thin, and says so by being nearly
empty rather than by inventing content:

```markdown
# epic_alert_burden_index

`{database}.gold.epic_alert_burden_index` · dynamic_table

---
dbt model `models/phrasehealth/global/epic_alert_burden_index.sql`
```

That is a feature. A 158-byte page tells the agent honestly that nothing is known
about this model, which should push it toward a documented alternative or toward
`describe_table`. It also makes the documentation gap visible to us (§6).

#### 2.1.4 Where provenance sits

Provenance is a fact about how the data was built, not about the rows in it and not
about anyone's opinion, so it is structural and belongs in tier 1. It splits into
two halves with very different costs.

**Table-level provenance is free and ships now.** Clarity `HV_ORDER_PROC` →
`{bronze}.hv_order_proc` → the gold models that consume it, from `depends_on` plus
the parsed Clarity headers. That is §2.1.2 and the lineage block in §2.1.3, and it
already answers "where does this table come from" in both directions.

**Column-level provenance is not currently available anywhere.** Only **62 of 6,452
columns** carry a description naming a `(model.column)` source — the
`alert_catalog_v2` page above, where every column cites
`alert_burden_index.thirty_day_index`, is the exception rather than the pattern.
`depends_on` is model-level only, so there is nothing to traverse. The requirement
is explicitly bidirectional — look up a Clarity column, see where it lands in gold —
and that needs real column-level lineage.

**Recommendation: buy it into tier 1.** Add a `dbt compile` step (the committed
`target/` holds no compiled SQL; `dbt parse` does not emit it) and derive
column-level edges from the resolved SQL with a parser such as sqlglot's lineage
module. This is the one tier-1 item that requires a new build dependency, and it is
worth it: the alternative is scraping descriptions, which yields ~1% coverage and
lands the result in tier 5, where it is an assertion the agent has to second-guess.
The §1.3 schema bug is the precedent — this design keeps getting hurt in exactly the
places where structure is inferred from convention rather than derived.

Until that lands, column-level provenance is **absent**, not approximated. A column
page that cannot state its source says nothing, the way §2.1.3's sparse pages do.

### 2.2 Tier 2 — Join paths

Promoted above the profiling tiers because a wrong join silently produces a wrong
number, which is the failure this whole design exists to prevent.

**It cannot be derived from declarations.** The repo has almost none:

| declared join evidence | count |
|---|---|
| `relationships` tests | **2** across 758 models |
| `primary_key` constraints | **1** |
| foreign key constraints | **0** |
| models with a `primary_key` config | 65 / 758 |

**And name-matching is actively misleading.** The most-shared column names across
models are ETL plumbing, not keys:

```
LINE                 194 models      EXTRACT_DATE        160
FILE_LAST_MODIFIED   123             INGEST_ID           111
```

while the real Clarity keys are sparse — `PAT_ENC_CSN_ID` in 32 models,
`ORDER_PROC_ID` in 6. A join section built by name-matching would recommend joining
on `LINE`, Clarity's row-index column.

**So join paths are established empirically.** For each candidate pair — column
names that match, restricted to keys, after excluding a plumbing denylist — the
profiling job measures value-domain overlap and the cardinality ratio in both
directions, and emits only the pairs that actually hold, with the measured
selectivity and the date the check ran:

```markdown
## Join paths — tier 2 (measured 2026-09-14)

- `alert_events.pat_enc_csn_id` → `phrase_encounter_expanded.pat_enc_csn_id`
  many-to-one · 98.7% of left values matched · 1.0 right rows per left key
- `alert_events.alert_id` → `dim_alert.alert_id`
  many-to-one · 100% matched · 1.0 right rows per left key
```

The overlap percentage is the point. A join that matches 98.7% of rows is usable
with a caveat; one that matches 12% means the agent has the grain wrong, and saying
so is more useful than silently emitting the join.

This tier is the one place the framework folds back on itself: join paths rank above
tiers 3 and 4 in trust, but are *produced by* the same scan pass, since measuring
overlap means reading the columns anyway.

### 2.3 Tier 3 — Shape and freshness

Answers "is this table usable at all," which no amount of schema can tell you. Two
sources, different costs, same question.

**Free, from `INFORMATION_SCHEMA`:** `ROW_COUNT`, `BYTES` and `LAST_ALTERED` per
table, plus `DATA_TYPE` and `IS_NULLABLE` per column — metadata only, no table scan.
This half should be **mandatory rather than optional**; it costs nothing and it is
also what supplies tier 1's types (§3). One caveat: `ROW_COUNT` is null for views,
which is 126 of 758 models.

**One cheap scan, and the highest-value fact in the design:** `MAX()` over each
table's business date column. `LAST_ALTERED` tells you when dbt last wrote, not
whether the data is current — an incremental model that has been running nightly
against a dead upstream feed looks perfectly healthy by that measure.

```markdown
## Shape — tier 3 (measured 2026-09-14)

`{database}.gold.orders` · 100 rows · last written 2026-09-14
**latest `order_dttm` is 2024-03-05** — 18 months stale
```

A stale partition is the failure mode that produces a confident, wrong report, and
nothing downstream catches it. Ranked above column profiles for that reason: knowing
a table stops in March 2024 changes whether you use it at all, while knowing the
top-10 values of a column only changes how you filter it.

### 2.4 Tier 4 — Column profiles

Full column scans: null rate, distinct count, and the ten most common values. This is
what makes filters correct — knowing that `order_status` holds `'AC'`/`'DC'` rather
than `'Active'`/`'Discontinued'` saves a round trip and a wrong `WHERE` clause.

Genuinely optional, genuinely expensive, and refreshed on a slower cadence than tier 3
because value domains move more slowly than row counts. Scope it to the gold surface
first; profiling all 6,452 documented columns across 758 models is not a starting
position.

Strictly drill-down: a full profile for one wide model is comparable in size to the
entire tier-1 index, so it is read per-table, never loaded broadly.

### 2.5 Tier 5 — Query strategy

Authored, non-deterministic, and the only tier a human writes by hand: *"for
referrals, join orders to encounters and filter on `order_type_c` in (…)"*. It
encodes the institutional knowledge that makes a query correct rather than merely
valid, and none of it is recoverable from the warehouse.

Two rules, both consequences of it being asserted rather than derived:

**It must be labelled as tier 5 where the agent reads it.** A strategy note that
has drifted is worse than no note, because it is stated with the same confidence as
tier 1. Marking the tier lets the agent verify a claimed filter against tiers 3–4
before building a report on it.

**It needs an owner and a review cadence,** or it becomes a graveyard of advice that
was true once. Curated in-repo, reviewed like code.

### 2.6 The escape hatch — raw model SQL

Outside the tiers, because it is not curated context at all — it is the source, read
directly when every tier has failed to answer a question about computation. The
model page footer names the exact path, so the agent reads one known file instead of
globbing. Whether we ship `models/` at all is a separate delivery question (§5); the
catalog is useful without it.

---

## 3. Rules the generator enforces

These are the places where a naive dump of the manifest would produce confidently
wrong output. All of them are tier-1 concerns.

**Discard both halves of `relation_name`; keep only the identifier.** The manifest
was parsed with `--target uvm_gold`, so every `relation_name` reads
`uvm.gold.<alias>` — wrong database for us (`.env` points at `penn`) and wrong
schema for 591 of 758 models (§1.3). Emitting it verbatim would point SQL at
another customer's database, in a schema the table isn't in. The generator takes
`alias` only and rebuilds the relation from the two rules below.

**Database is a placeholder resolved per deployment.** Catalog files carry
`{database}`; it is substituted at sync time from `SNOWFLAKE_DATABASE`, and
`CLAUDE.md` states that the database comes from the connection, never the catalog.

**Schema is derived from the layer directory, via a configured mapping.** The
generator reads the layer segment of `path` (`phrasehealth/<layer>/…`) and looks it
up in a layer→schema map, rather than trusting `schema` or hardcoding `bronze` /
`silver` / `gold` as literals. The map's source of truth is `profiles.yml`, where
target `<org>_<layer>` declares the schema; the generator can parse it or take it
as a CLI argument. This is the seam that makes the catalog portable (§5.1).

**Column types come from `INFORMATION_SCHEMA`, not the manifest.** `data_type` is
`None` for all 6,452 columns in the manifest — it is only populated by
`dbt docs generate`, which queries Snowflake. Rather than take that dependency, tier 1
reads types and nullability from `INFORMATION_SCHEMA.COLUMNS`, which is metadata-only
and free (§2.3). Bronze is the one place the manifest wins: the pre-hook DDL states
Clarity's own declared types literally, which is what a user asking in Clarity terms
wants to see, so those are lifted from the DDL (§2.1.2).

The live `describe_table` tool stays, but as a check rather than the primary source —
existence and columns are always confirmable against the warehouse mid-conversation.

**PHI tags are surfaced at selection time**, not left in the dbt metadata.

---

## 4. Retrieval cost, worked

*"How many alerts fired in the last 30 days, and what share were overridden?"*

| | raw repo | catalog |
|---|---|---|
| find candidates | `Glob **/*.sql` → 759 paths, or `Grep alert` → 98 files | index already in context |
| identify the model | read several model files; Jinja, no relation name | one line names `alert_catalog_v2` |
| get columns | read `alerts/schema.yml`, 84 KB ≈ 21k tokens | read one page, 4 KB ≈ 1k tokens |
| learn the relation | not derivable from the repo | stated on the page |
| turns consumed | 5–15, variable | 1 |
| repeatability | different subset each run | deterministic |

Both columns above are tier 1. Tier 3 would add *"is `alert_events` still being
written?"* and tier 2 *"does `alert_events` join to `dim_alert` one-to-one?"* —
questions the raw-repo column cannot answer at any token cost, because the answers
are not in the repo.

*"Pull the discrete frequency on procedure orders — the `ORDER_PROC` stuff."*

| | raw repo | catalog |
|---|---|---|
| map Clarity name → relation | not in the graph; no dbt sources exist | `clarity.md` row: `HV_ORDER_PROC` → `bronze.hv_order_proc` |
| know whether a modeled table is better | not derivable | `— query directly`: this is the only source |
| get columns and types | `describe_table`, or parse pre-hook DDL live | on the page, from the DDL |
| failure mode if it guesses | manifest says `gold.hv_order_proc`, which does not exist (§1.3) | — |

The catalog path also front-loads its cost into a cacheable prefix, where the
Glob/Grep path pays fresh tokens on every conversation.

---

## 5. Freshness and delivery

### 5.1 Portability across databases and schemas

The warehouse is multi-tenant: `profiles.yml` defines 17 databases — `penn`, `uvm`,
`CHOP`, `FLPEN`, `TXAUS`, … — each with its own role, across both the Epic and
Cerner projects. A deployment points at one. So the catalog has to be a per-project
artifact with per-deployment substitution, and the generator must not bake in any
one tenant's names.

Three things vary, and each has one designated seam:

| varies | today | seam |
|---|---|---|
| database | 17, one per org | `{database}`, substituted at sync from `SNOWFLAKE_DATABASE` |
| schema names | uniformly `bronze` / `silver` / `gold` across all 49 targets | layer→schema map, read from `profiles.yml` or passed in (§3) |
| model set | 758 Epic, 123 Cerner | one catalog per project, built from that project's manifest |

**Schema names are uniform today, and that is a convention rather than a
guarantee.** All 49 targets in `profiles.yml` use exactly `bronze`, `silver`, `gold`,
and `bronze_cross_schema_ref` hardcodes `target_schema='BRONZE'` as its default — so
the names are baked into the transforms repo's macro layer, not just its config. A
new tenant database gets these schemas by construction. The generator still takes the
mapping as input rather than assuming it, because the cost of the seam is one
argument and the cost of hardcoding is a silent wrong-schema bug of exactly the kind
§1.3 describes.

**What genuinely would not survive** is a warehouse that is not layered
one-schema-per-layer at all — a tenant that put everything in one schema, or split
by domain instead of layer. Then the layer→schema map stops being a function of the
directory and the derivation rule itself has to change, not just its inputs. That is
a real limit and worth stating: the catalog assumes layer-per-schema, and is
parameterised within that assumption, not beyond it.

### 5.2 Build and delivery

`target/manifest.json` is a dbt build artifact (6.9 MB), produced by `dbt parse`.
The generator is a pure function of it, so it belongs in the transforms repo's CI:

```
transforms CI:  dbt parse --target <org>_gold
                  →  build_catalog.py --manifest … --layer-schemas …
                  →  publish catalog/<project>/ to S3
report-agent:   sync s3://…/catalog/<project>/ → $WORKSPACE_DIR/catalog,
                  substituting {database} from SNOWFLAKE_DATABASE
```

One catalog per dbt project (`epic`, `cerner`), not per tenant database: the model
set is a property of the project, and the only per-tenant value is the database
name, substituted at sync (§5.1).

That pipeline builds tier 1 only. The tiers have different triggers, owners and
blast radii, which is the practical payoff of separating them:

| tier | trigger | needs a warehouse | owner |
|---|---|---|---|
| 1 | transforms merge | no (except `INFORMATION_SCHEMA`) | data eng CI |
| 2 | with the profiling job | yes, one scan pass | data eng CI |
| 3 | nightly | yes, cheap | scheduled job |
| 4 | weekly | yes, expensive | scheduled job |
| 5 | pull request | no | whoever owns reporting |

Tiers 2–4 are **per-tenant**, not per-project: row counts and join selectivity in
`penn` say nothing about `TXAUS`. That is the one place the per-project artifact
model breaks down, and it means a tenant with no profiling job simply has tiers 1
and 5 — which the tiering makes a graceful degradation rather than a broken
deployment. Note that the parse target is incidental — the
generator discards the database and schema it stamps (§3) — but a target must be
named for `dbt parse` to run.

**Not a git submodule.** A submodule pins a SHA, so every transforms change needs a
commit in *this* repo to take effect; it couples the data team's release cadence to
the app's deploy cadence for no benefit, and it ships 9,728 files to deliver ~800
models' worth of metadata. The artifact pipeline decouples them: transforms merges,
CI republishes, the next container start picks it up. The task role already has S3
access for report output.

**Staleness is bounded by design.** Between a transforms merge and a catalog
refresh, the agent may not know about a new model (it will say so) or may believe a
dropped column exists (Snowflake returns an error, which `run_sql` surfaces to the
model to retry). Neither failure silently produces a wrong number, which is the
only failure mode that actually matters for a reporting tool.

---

## 6. What this does not solve

**Documentation gaps become visible, not fixed.** 20 of the 167 gold models have no
description and 6 have no documented columns — the five `global/epic_*` models among
them. The catalog renders them as near-empty pages. The fix belongs in the dbt repo's
`schema.yml`, and the catalog build is a good place to report the coverage number so
it can be tracked.

**Metric definitions beyond the descriptions.** "What counts as an override" is
answered well by `alert_override_rate_annual`'s description today, but that is luck,
not structure. If report-level metric definitions need to be authoritative, they
want their own curated file in the workspace rather than being scavenged from model
docs.

**Which project a deployment should see.** §5.1 settles how to build per-project
catalogs; it does not settle whether a deployment is always exactly one project, or
how the app would be told. `SNOWFLAKE_DATABASE` implies it today (`penn` is Epic,
`TXAUS` is Cerner) but only by convention, and nothing checks it.

**Tiers 2–4 are specified, not built.** This document fixes what they contain, what
they cost and when they refresh; it does not settle the profiling job itself — how
candidate join pairs are enumerated without an O(n²) sweep over 2,970 distinct column
names, what the plumbing denylist contains, or how much warehouse budget a nightly
tier-3 pass is allowed. Tier 1 is buildable today and is worth shipping before any of
that is resolved.

**Clarity coverage stops at the table name.** The crosswalk maps Clarity *tables*;
it does not carry Epic's own column semantics (what `STND_TP_C` means, which
category list a `_C` column points at). Five of 261 bronze models also lack the
`-- Epic <TABLE>` header and fall back to the model name, which is right in every
case checked but is an inference, not a declaration. The durable fix is to declare
these as dbt `sources` with descriptions — then the crosswalk comes from structure
instead of from parsing comments, and the lineage graph gains the Clarity edge it
is missing today.

**Cross-schema grants are unverified.** The catalog will hand the agent `bronze.`
and `silver.` relations, where today `.env` sets `SNOWFLAKE_SCHEMA=gold` and
`SNOWFLAKE_ROLE=PUBLIC`. Whether the reporting role can read the other two schemas
is untested — `scripts/check_snowflake.py` only probes the configured one — and if
it can, nothing in the read-only guard distinguishes raw Clarity from modeled gold.
Both halves want deciding deliberately: the grants, and whether the agent should be
told to prefer gold when a gold model exists.
