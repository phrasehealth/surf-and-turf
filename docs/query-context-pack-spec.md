# Query Context Pack (QCP) — format specification

**Version:** 1.0
**Status:** draft
**Audience:** engineers and agents building a QCP extractor for a database; agents consuming a QCP to write queries

A **Query Context Pack** is a directory of plain-text files describing one database,
optimized for an AI agent that writes queries against it. It is not a data catalog
(those are human discovery UIs) and not a semantic layer (those define metrics). It
is the briefing an agent reads before writing SQL.

A pack is **produced** by an extractor, from whatever sources a given database has,
and **consumed** by a query-writing agent. Both sides are specified here. The pack
is source-agnostic: nothing in this format assumes a particular warehouse,
transformation tool, naming convention, or layering scheme.

---

## 1. Design principles

**1.1 Grep and glob are the query interface.** An agent finds things in a pack with
`Grep`, `Glob` and `Read` — not by loading it all, and not through an API. Three
rules follow, and they govern every file format below:

- **Every line is self-addressing.** A grep hit must be interpretable on its own. A
  line describing a column names its table; a line describing a join names both
  sides. Never require the reader to scan upward for context.
- **One fact per line, no wrapping.** A fact never spans two lines. Descriptions are
  collapsed to a single line, whitespace normalized.
- **Paths are predictable.** A reader who knows a table's name knows its file path
  without listing a directory.

**1.2 Every fact carries its evidence class.** A fact introspected from the database
and a fact asserted by a human are both useful and must never be confused. Each
carries a marker (§4.3) so the consumer can weigh it and verify when it matters.

**1.3 Structure is derived, never inferred from convention.** Where the database can
be asked directly, ask it. Naming conventions, directory layouts and file paths are
evidence of intent, not statements of fact, and an extractor that treats them as
fact will be confidently wrong. See §7.4.

**1.4 Absent beats approximate.** A pack omits what it does not know. An empty
section is a true statement; a plausible guess is a trap, because the consumer
cannot distinguish it from a derived fact.

**1.5 Degradation is graceful.** A pack built from introspection alone is valid and
useful. Every other input improves it. Conformance levels (§6) make the floor
explicit.

---

## 2. Scope and assumptions

This format assumes the described system:

- exposes **relations** (tables, views, or equivalent) that contain **columns**;
- is queryable in a text query language (SQL or similar);
- can be introspected for its list of relations and columns.

It does **not** assume: a specific warehouse or engine; a transformation framework;
a medallion/layered architecture; any particular schema names; that schemas carry
meaning; that descriptions exist; or that lineage is recorded anywhere.

---

## 3. Directory layout

```
<pack>/
  README.md                       consumer instructions (§5.1)
  MANIFEST.md                     what this pack is, when built, what it covers (§5.2)
  index.md                        one line per relation — standing context (§5.3)
  columns.tsv                     one line per column (§5.4)
  relations/<schema>/<relation>.md   one page per relation (§5.5)
  joins.tsv                       one line per join path (§5.6)
  lineage.tsv                     one line per lineage edge (§5.7)
  aliases.tsv                     external-vocabulary crosswalk (§5.8)
  profiles/<schema>/<relation>.md    value profiles (§5.9)
  concepts/<slug>.md              authored query strategy (§5.10)
  sources/<schema>/<relation>.<ext>  verbatim definition source, optional (§5.11)
```

**Path rules.** `<schema>` and `<relation>` in a path are the **normalized
identifier** (§4.1). Directories exist only if non-empty. A pack with no
`concepts/` is valid.

---

## 4. Common conventions

### 4.1 Identifiers

Every identifier appears in a pack in **normalized** form: lowercased, with
surrounding quotes removed. Normalized identifiers are used in file paths, in TSV
key columns, and in prose.

If a relation or column's real identifier differs from its normalized form in a way
that matters for querying — because the database is case-sensitive, or the
identifier requires quoting — the pack records the exact form in a `raw_name` field
and the consumer uses that when emitting SQL. If normalization is lossless (the
common case for case-insensitive engines), `raw_name` is omitted.

A relation is addressed as `<schema>.<relation>`. A column is addressed as
`<schema>.<relation>.<column>`. These forms are what the consumer greps for, so
they must be written identically everywhere they appear.

### 4.2 The database placeholder

A pack describes the *shape* of a database, which is usually shared across
deployments (dev/prod, or per-tenant copies), while the database name is not.
Packs therefore write the literal token `{database}` wherever a fully-qualified
name is needed:

```
{database}.gold.alert_events
```

The consumer substitutes its own connection's database. An extractor **must not**
emit the database it happened to introspect. If a pack is genuinely single-database,
`MANIFEST.md` may set `database_fixed: <name>` and use it literally.

### 4.3 Evidence classes

Every fact in a pack carries one of these markers. They are ordered by how much a
consumer should trust them unverified:

| class | meaning | example source |
|---|---|---|
| `introspected` | read from the database's own catalog | `INFORMATION_SCHEMA`, `DESCRIBE` |
| `declared` | stated in a transform or schema definition | dbt `schema.yml`, a DDL constraint, an ORM model |
| `measured` | observed by running a query against the data | `MAX(date)`, join overlap %, `COUNT` |
| `derived` | computed deterministically from another artifact | lineage from parsed SQL |
| `inferred` | produced by a heuristic that can be wrong | a join guessed from matching column names |
| `authored` | written by a human or model | a query-strategy note |

**`inferred` and `authored` facts must be visibly marked wherever the consumer will
read them**, not only in a TSV column. A consumer treats them as hypotheses.

### 4.4 Timestamps

All timestamps are ISO-8601 dates or datetimes in UTC. Any `measured` fact carries
`measured_on`. Every generated file's header carries the pack build time. A consumer
uses these to decide whether to re-verify.

### 4.5 TSV files

- Tab-separated. No quoting, no escaping: tabs and newlines are stripped from field
  values at write time, and any other character is written literally.
- First line is a header naming the columns, exactly as specified below.
- One record per line. Field order is fixed. Unknown values are the empty string,
  including trailing ones — every line has the same number of tabs as the header.
- Sorted by the leading key columns, so diffs between builds are readable.

TSV is used wherever a fact set is searched *across* relations. Markdown is used
wherever a human or agent reads *about* one relation.

### 4.6 Descriptions

A description is a single line: internal whitespace collapsed, newlines removed,
truncated to 200 characters at a word boundary with a trailing `…` if it was longer.
The untruncated text, if any, lives on the relation's page (§5.5).

---

## 5. File specifications

### 5.1 `README.md` — consumer instructions

**Required.** The pack carries its own usage instructions so that any agent handed
the directory can use it without external configuration. It is the first thing a
consumer reads.

It must state: what database the pack describes; the lookup protocol (§8); the
meaning of the evidence classes present; the substitution rule for `{database}`;
and what the consumer must verify live rather than trust. An extractor generates
this file from a template and fills in the pack-specific values.

### 5.2 `MANIFEST.md` — pack metadata

**Required.** Stable `key: value` lines, one per line, greppable:

```markdown
# Query Context Pack — penn

qcp_version: 1.0
pack_name: penn
built_at: 2026-09-17T14:02:11Z
database_placeholder: {database}
schemas: bronze, silver, gold
relations: 781
columns: 11404
conformance: L3
evidence_present: introspected, declared, measured, derived, authored
sources:
  - introspection: snowflake information_schema @ 2026-09-17T14:00:02Z
  - transforms: dbt manifest v12 @ 2026-09-16T22:15:00Z
  - profiling: qcp-profiler v1.0 @ 2026-09-17T03:00:00Z
coverage:
  descriptions: 710/781 relations, 6452/11404 columns
  joins: 143 verified, 0 inferred
  freshness: 87/171 relations in gold have a date column
  profiles: 42 relations
```

`coverage` is not decoration. It tells the consumer where the pack is thin, which is
what stops it trusting silence as absence.

### 5.3 `index.md` — the standing context

**Required.** The only file a consumer is expected to hold in context for every
conversation. One line per relation, grouped by schema under `##` headings.

```
- `gold.alert_events` · 1.66B rows · fresh 2026-09-16 · Alert events with classification and interruptivity logic.
- `gold.alert_catalog_v2` · 4.6K rows · fresh 2026-09-16 · Denormalized alert catalog assembled from dim_alert and the metric facts.
- `bronze.alert_criteria` · 0 rows · EMPTY · Epic ALERT_CRITERIA, landed by ingest.
```

**Line grammar:**

```
- `<schema>.<relation>` · <rows> · <freshness> · <description>
```

- `<rows>`: human-scaled count (`0`, `4.6K`, `1.66B`) or omitted if unknown.
- `<freshness>`: `fresh <date>` for the latest business date; `EMPTY` for a
  zero-row relation; `STALE <date>` when the latest business date is older than the
  pack's staleness threshold; omitted if unknown.
- `<description>`: per §4.6.

**Sizing.** `index.md` must stay small enough to be standing context. If the
database is too large, the extractor narrows the index by a documented rule — a
schema allowlist, a consumption-layer filter, an excluded-prefix list — and
`MANIFEST.md` records which relations were omitted and why. **Omitted relations must
still appear in `columns.tsv` and have a page**, so they remain findable by grep;
only the standing summary is narrowed.

### 5.4 `columns.tsv` — the primary grep target

**Required.** One line per column, in every relation, including those omitted from
`index.md`. This is the file that answers "the user said `PAT_ENC_CSN_ID`."

```
schema	relation	column	type	nullable	raw_name	evidence	description
gold	alert_events	pat_enc_csn_id	NUMBER	NO		introspected	Patient encounter CSN identifier
gold	alert_events	alert_id	NUMBER	NO		introspected	Alert identifier
bronze	hv_order_proc	stnd_tp_c	NUMBER	YES		declared	Standing order type category
```

Sorted by `schema`, `relation`, then ordinal position (not alphabetically — column
order carries meaning). `type` is the engine's own type name, verbatim.

### 5.5 `relations/<schema>/<relation>.md` — the relation page

**Required for every relation.** What the consumer reads once it has chosen a table.

```markdown
# gold.alert_events

`{database}.gold.alert_events` · base table · 1,662,064,062 rows
grain: one row per alert firing
freshness: latest `contact_date` 2026-09-16 (measured 2026-09-17)
tags: contains-phi
alias: clarity `BPA_FUP_SIGNED_ORD` (derived)

Alert events with classification and interruptivity logic. Aggregates alert data
with complex type determination. Refreshed daily.

## Columns

| column | type | null | description |
|---|---|---|---|
| `alert_id` | NUMBER | NO | Alert identifier |
| `pat_enc_csn_id` | NUMBER | YES | Patient encounter CSN identifier |
| `contact_date` | DATE | NO | Date the alert fired |

## Joins

- `alert_id` → `gold.dim_alert.alert_id` · many-to-one · 100% matched · measured 2026-09-14
- `pat_enc_csn_id` → `gold.phrase_encounter_expanded.pat_enc_csn_id` · many-to-one · 98.7% matched · measured 2026-09-14

## Lineage

upstream: `silver.alert_v2_deduped`, `silver.alert_history_v2_deduped`
downstream: `gold.alert_events_expanded`, `gold.alert_burden_index`

## Definition

`sources/gold/alert_events.sql` — transform source, read for computation questions only
```

**Section rules.** Every section is optional except the header block and `Columns`.
A section that would be empty is omitted entirely rather than rendered empty. Each
`Joins` and `Lineage` line duplicates a row in the corresponding TSV — the TSV is
for cross-relation search, the page for reading in place. They are generated
together and must not disagree.

Where a fact's evidence class is weaker than `introspected`/`declared`/`measured`,
the line says so inline: `(inferred)` or `(authored)`.

### 5.6 `joins.tsv` — join paths

**Optional.** Absent when the extractor cannot establish joins; never populated by
guesswork alone.

```
left_schema	left_relation	left_column	right_schema	right_relation	right_column	cardinality	match_pct	evidence	measured_on
gold	alert_events	alert_id	gold	dim_alert	alert_id	many-to-one	100.0	measured	2026-09-14
gold	alert_events	pat_enc_csn_id	gold	phrase_encounter_expanded	pat_enc_csn_id	many-to-one	98.7	measured	2026-09-14
```

- `cardinality`: `one-to-one`, `many-to-one`, `one-to-many`, `many-to-many`, or empty.
- `match_pct`: percentage of left-side non-null values found on the right, to one
  decimal. Required when `evidence` is `measured`; empty otherwise.
- Each join is written **once per direction that is useful**; a consumer looking up
  either table must find it, so emit both rows rather than requiring a reverse scan.

**A `match_pct` well below 100 is information, not a reason to omit the row.** A join
matching 12% tells the consumer its grain assumption is wrong, which is more useful
than silence.

**On inferring joins.** An extractor may emit `inferred` joins from a heuristic
(matching names and compatible types) only if it also emits a denylist-aware
rationale, and only when no measurement is possible. Column-name matching is
dangerous: in a typical warehouse the most-shared column names are ingest plumbing
(`line`, `extract_date`, `ingest_id`, `updated_at`), not keys. If the extractor
cannot verify, prefer omitting.

### 5.7 `lineage.tsv` — provenance edges

**Optional.** Two record kinds in one file, distinguished by whether the column
fields are populated. Relation-level edges have empty column fields.

```
downstream_schema	downstream_relation	downstream_column	upstream_schema	upstream_relation	upstream_column	transform	evidence
gold	alert_events		silver	alert_v2_deduped			derived
gold	alert_catalog_v2	burden	gold	alert_burden_index	thirty_day_index	rename	derived
```

- `transform`: a short label for what happened to the value — `rename`, `cast`,
  `aggregate`, `derive`, `passthrough` — or empty if unknown.
- Relation-level lineage is usually cheap (most transform tools record it).
  Column-level lineage usually requires parsing resolved query text; it is
  `derived` when parsed and **must not** be `inferred` from name matching.

Provenance is bidirectional by construction: the consumer greps this file for a
downstream address to find sources, or an upstream address to find destinations.

### 5.8 `aliases.tsv` — external vocabulary crosswalk

**Optional, and high value wherever users think in a source system's names.**
Generalizes any situation where the same entity is known by a different name outside
the warehouse — an EHR's table names, a CRM's object names, a legacy system, a
business glossary.

```
vocabulary	external_name	schema	relation	column	evidence	note
clarity	HV_ORDER_PROC	bronze	hv_order_proc		derived	1:1 ingest passthrough
clarity	PAT_ENC_CSN_ID			pat_enc_csn_id	derived	appears in 32 relations
salesforce	Opportunity	raw	sf_opportunity		declared	
```

- `vocabulary` names the external namespace; a pack may carry several.
- Either `relation` or `column` may be empty: a row can map an external table name,
  an external column name, or both.
- The consumer greps this file **first** when a user's term does not appear in
  `index.md`.

### 5.9 `profiles/<schema>/<relation>.md` — value profiles

**Optional.** Expensive to produce, read only on demand, never standing context.

```markdown
# Profile — gold.alert_events
measured_on: 2026-09-14 · rows sampled: full

| column | nulls | distinct | top values |
|---|---|---|---|
| `alert_type_c` | 0.0% | 5 | `LGL` (61%), `EMI` (22%), `MED` (9%), `MAR` (5%), `DIS` (3%) |
| `contact_date` | 0.0% | 2,841 | min 2018-11-02, max 2026-09-16 |
| `override_reason_id` | 74.1% | 312 | `1` (18%), `7` (11%), `22` (9%) |
```

Value profiles are what make filters correct: knowing a status column holds `AC`
and `DC` rather than `Active` and `Discontinued` saves a wrong `WHERE` clause. For
date and numeric columns, min/max is more useful than top values.

The extractor must record whether the profile is from a full scan or a sample, and
the sample size if sampled.

### 5.10 `concepts/<slug>.md` — authored query strategy

**Optional.** The only hand-written part of a pack, and the only part that can be
wrong in a way no rebuild will fix.

```markdown
# Referrals
evidence: authored · owner: analytics@example.com · reviewed: 2026-08-30

A "referral" is not a relation. Join `gold.orders` to `gold.encounters` on
`pat_enc_csn_id` and filter `order_type_c in (12, 44)`. Exclude
`order_status_c = 'DC'` unless the question is explicitly about cancellations.

Known trap: `gold.orders_v1` still exists and has a different `order_type_c`
encoding. Use `gold.orders`.
```

**Every concept file must carry `evidence: authored`, an owner, and a review
date.** A strategy note with no owner becomes a graveyard of advice that was true
once, and the consumer has no way to tell. Files whose review date is older than
the threshold in `MANIFEST.md` should be flagged by validation (§9), not silently
served.

Concepts are found by `Glob concepts/*.md` and by grepping their titles and bodies,
so slugs should be the words a user would actually say.

### 5.11 `sources/<schema>/<relation>.<ext>` — verbatim definitions

**Optional.** The transform source (SQL, Python, a stored procedure body) for a
relation, copied verbatim. The escape hatch: read only when a question is about how
a value is computed and no other tier answers it. Referenced by exact path from the
relation page, so the consumer never searches for it.

---

## 6. Conformance levels

A pack declares its level in `MANIFEST.md`. Levels are cumulative.

| level | requires | answers |
|---|---|---|
| **L0** | `README.md`, `MANIFEST.md`, `index.md`, `columns.tsv`, relation pages | what exists, what columns, what types |
| **L1** | + descriptions on relations and columns where available | what things mean |
| **L2** | + `lineage.tsv`, and `aliases.tsv` if an external vocabulary exists | where data came from, what users' terms map to |
| **L3** | + row counts and freshness in `index.md` and relation pages | whether a relation is usable at all |
| **L4** | + `joins.tsv` with measured joins, `profiles/` | how to connect relations and filter them correctly |

**L0 is achievable against any database with introspection alone** and is a useful
pack. Each level after it depends on an input the database may not have.
`concepts/` is orthogonal and may be added at any level.

---

## 7. Extractor contract

An extractor reads whatever sources a database has and emits a conforming pack.
This section is the instruction set for building one against a novel database.

### 7.1 Inputs, in priority order

1. **Catalog introspection** — required. `INFORMATION_SCHEMA`, `pg_catalog`,
   `DESCRIBE`, a driver's metadata API. Supplies the authoritative list of relations,
   columns, types, nullability, and usually row counts and modification times.
2. **Transform metadata** — optional. A dbt/SQLMesh/Dataform manifest, migration
   files, ORM models, stored-procedure definitions. Supplies descriptions, tags,
   grain declarations, lineage, and the source text for `sources/`.
3. **Documentation** — optional. Schema YAML, data dictionaries, README files,
   glossaries, spreadsheets. Supplies descriptions and external vocabulary.
4. **The data itself** — optional. Required only for L3 freshness, L4 joins and
   profiles.
5. **A prior pack** — optional. Lets an extractor preserve `concepts/` and diff
   coverage across builds.

### 7.2 Authority rules

These resolve conflicts between inputs, and they are the heart of the contract:

- **Introspection is authoritative for existence, names, types and nullability.**
  If the transform metadata describes a relation that introspection does not show,
  the relation does not exist — record it under `coverage` as a discrepancy, do not
  emit it as real.
- **Transform metadata and docs are authoritative for meaning** — descriptions,
  grain, tags, intent. Introspection cannot supply these.
- **Measurement is authoritative for state and for joins.** No declaration about
  freshness or cardinality outranks an observation.
- **Never take a fully-qualified name from a compiled artifact.** Transform
  manifests are usually compiled against one target, and carry that target's
  database and schema. Those values are a property of the build, not of the model.
  Take the identifier only, and resolve schema from introspection.

### 7.3 Required behaviors

- Emit `{database}` placeholders, never a concrete database name (§4.2).
- Mark every fact with its evidence class (§4.3).
- Reconcile introspection against transform metadata and record both directions of
  discrepancy in `MANIFEST.md` under `coverage`.
- Normalize identifiers, and record `raw_name` only where normalization is lossy.
- Emit a relation page and `columns.tsv` rows for **every** relation introspected,
  even those excluded from `index.md`.
- Sort every TSV deterministically, so two builds of unchanged inputs are identical.
- Be idempotent, and preserve `concepts/` across rebuilds.

### 7.4 Prohibited behaviors

- **Do not derive schema, database, or existence from file paths, directory names
  or naming conventions.** A transform repo's directory layout may correlate with
  schemas, but the correlation is a property of how the build is invoked, not
  something the files state. Ask the database.
- **Do not emit inferred joins as if measured**, and do not emit name-matched joins
  at all when measurement is available.
- **Do not fabricate descriptions.** A relation with no description gets no
  description. A summary generated by a model is `authored` and must be marked.
- **Do not let a partial failure produce a silent partial pack.** If profiling
  fails, the pack drops to L3 and says so in `MANIFEST.md`.

### 7.5 Suggested build order

Cheap and deterministic first, so a useful pack exists before expensive work starts:

```
introspect  → columns.tsv, relation pages, index.md skeleton   (L0)
transforms  → descriptions, tags, grain, lineage.tsv, sources/ (L1–L2)
docs        → aliases.tsv, remaining descriptions              (L2)
metadata    → row counts, last-modified                        (L3, cheap)
measure     → freshness MAX(date), then joins, then profiles   (L3–L4, costly)
```

Freshness before joins before profiles: freshness is the cheapest measurement and
prevents the worst failure (a confident report from a dead table); profiles are the
most expensive and prevent the mildest (an extra round trip on a filter).

---

## 8. Consumer contract

How a query-writing agent uses a pack. An extractor writes this protocol into the
pack's `README.md`, adapted to what that pack actually contains.

### 8.1 Standing context

Load `MANIFEST.md` and `index.md` only. Everything else is on demand. Never load
`columns.tsv`, a profile, or the whole `relations/` tree into context.

### 8.2 Lookup protocol

1. **A term from the question that looks like a table** → search `index.md`.
2. **A term that does not appear there** → `Grep` `aliases.tsv`, then `columns.tsv`.
   Users name columns and source-system objects at least as often as they name
   warehouse tables.
3. **A chosen relation** → `Read relations/<schema>/<relation>.md`. **Not optional.**
   `index.md` deliberately carries no column names (§5.3), so it is a sufficient-feeling
   stopping point that cannot support a query. The relation page is the only file needed
   to write a single-relation query, and it must be read before one is written.
4. **A second relation** → `Grep joins.tsv` for both addresses before writing any
   `JOIN`. If no row exists, the join is unverified — say so, and prefer a
   single-relation answer or ask.
5. **A filter on an unfamiliar column** → `Read profiles/<schema>/<relation>.md`
   if present; otherwise `SELECT DISTINCT` a small sample before committing.
6. **"Where does this come from" / "where does this end up"** → `Grep lineage.tsv`
   for the address.
7. **A question about how a value is computed** → the `Definition` path on the
   relation page. Last resort.
8. **A domain term that is not a table or column** (a "referral", a "readmission")
   → `Glob concepts/*.md` and read the match.

### 8.3 Trust and verification

- Treat `introspected`, `declared`, `derived` and `measured` facts as reliable.
- Treat `inferred` and `authored` facts as hypotheses. Verify before relying on
  them, and say in the answer that they were assumptions.
- If `MANIFEST.built_at` is older than the consumer's staleness threshold, confirm
  relation and column existence against the live database before the final query.
- **Never invent a column.** Before writing a column name in a query, the consumer
  must have read that exact name, for that exact relation, on the relation page, in
  `columns.tsv`, or from a live `DESCRIBE`. Plausibility is not evidence — a relation
  described as a "diagnosis reference" may name the column `dx_code` rather than
  `code`. If a needed column is in none of those places, say so rather than trying a
  likely spelling.

  An extractor must render this rule into the pack's `README.md` (§5.1). It is the
  rule consumers are most likely to skip, because finding the right relation feels
  like having finished the lookup.

### 8.4 Reporting obligations

When the answer is a report or a number that someone will act on, state the
relations used, and pass through any `EMPTY` or `STALE` marker on them. A freshness
caveat the pack supplied and the answer dropped is the single most damaging way this
system can fail: a confidently wrong number that nothing downstream catches.

---

## 9. Validation

A conforming pack passes these checks. A validator should be shipped with any
extractor.

**Structural**
- `README.md`, `MANIFEST.md`, `index.md`, `columns.tsv` exist; `MANIFEST.md` parses
  and declares `qcp_version` and `conformance`.
- Every relation in `columns.tsv` has a page at the path implied by its address, and
  every page corresponds to a relation in `columns.tsv`.
- Every TSV has the exact specified header and a constant field count per line.
- No field contains a tab or a newline. No line in any file wraps a single fact.

**Referential**
- Every address in `joins.tsv`, `lineage.tsv` and `aliases.tsv` resolves to a
  relation, and to a column where one is named.
- Join and lineage lines on a relation page match the corresponding TSV rows.

**Semantic**
- Every fact has an evidence class drawn from §4.3.
- Every `measured` fact has a `measured_on`.
- Every `inferred` or `authored` fact is visibly marked where it is read, not only
  in a TSV column.
- Every `concepts/*.md` file declares an owner and a review date.
- No file contains a concrete database name where `{database}` is required.

**Health** — warnings, not failures
- Relations in `index.md` with no description.
- `concepts/` files past their review threshold.
- Discrepancies between introspection and transform metadata.
- `index.md` exceeding the pack's declared standing-context budget.

---

## 10. Versioning

`qcp_version` is `MAJOR.MINOR`. A consumer written for `1.x` must tolerate unknown
files and unknown trailing TSV fields. Adding a file kind, an optional TSV field, or
an evidence class is MINOR. Changing a field's meaning, removing a required file, or
changing a line grammar is MAJOR.
