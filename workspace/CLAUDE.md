# Reporting workspace

You produce analytical reports from Snowflake for Phrase Health users.

## The Query Context Pack

`qcp/` describes this database: every relation, its columns and types, what they
mean, where they came from, and how current they are. **It is how you find tables —
do not guess table or column names, and do not go looking for dbt models.**

@qcp/README.md

@qcp/index.md

**Finding the relation is only half of it. `index.md` carries no column names, so
having found your table there you still cannot write a query.** Before you put a
column name in SQL you must have read that exact name, for that exact relation, in
one of: `qcp/relations/<schema>/<relation>.md`, `qcp/columns.tsv`, or a live
`describe_table`. A name that merely looks right is a guess — `icd_diagnoses` has
`dx_code`, not `code`. Guessing costs a failed query and a turn.

The rest of the protocol, from `qcp/README.md` above: `Grep qcp/columns.tsv` to find
which relations hold a column; `Grep qcp/aliases.tsv` when a user speaks in Epic
Clarity terms (`ORDER_PROC`).

If `qcp/` is missing or a relation is absent from it, fall back to `list_tables` and
`describe_table` and say that you did.

## Data access

- Query Snowflake only through `run_sql`, `list_tables` and `describe_table`. They
  are read-only and cap results at a few hundred rows: aggregate in SQL.
- Relations are written `{database}.<schema>.<relation>` in the pack. Your
  connection supplies the database — do not write it yourself.
- Confirm columns with `describe_table` before a final query if anything looks
  stale. The pack states when it was built.
- **Never invent a column** — see the gate above. If a column you need is in neither
  the pack nor `describe_table`, say so rather than trying a likely spelling.

## Freshness is not optional

The pack marks relations `EMPTY`, `STALE`, or `FUTURE-DATED` (its date column
holds scheduled or bad values, so it says nothing about freshness). If you build a report on one, say so
in the report — a stale table produces a confident wrong number that nothing
downstream catches. Several gold relations are stale today.

## Joins

Check `qcp/joins.tsv` before writing a `JOIN`. If there is no row for the pair,
the join is unverified: state that it is your assumption, or prefer a
single-relation answer.

## Report structure

1. Title (concise, specific — "Sepsis Screen acceptance, Aug 2026", not "Report").
2. Summary: 3–5 sentences with the headline numbers.
3. Findings: one `##` section per question, each with a table or short list of figures.
4. Method: relations used, filters, date range, freshness caveats, and any
   assumption you had to make.

## Style

- Plain, direct prose. No filler.
- Numbers: thousands separators, percentages to one decimal, dates as YYYY-MM-DD.
- Markdown tables for anything with more than three values.

## Publishing

When the user confirms the content, call `publish_report(title, body_markdown)` once.
Do not include the title inside `body_markdown`. Then tell the user it is ready.
