# Query Context Pack — how to use this

This directory describes one database for an agent that writes queries against it.
It conforms to Query Context Pack 1.0, conformance level **L0**.
Read `MANIFEST.md` for what it covers and where it is thin.

## Standing context

Load `MANIFEST.md` and `index.md` only. Everything else is on demand. Do not load
`columns.tsv`, a profile, or the whole `relations/` tree into context — they are
grep and glob targets.

## Lookup protocol

1. A term that looks like a relation → search `index.md`.
2. A term that is not there → `Grep` `aliases.tsv`, then `columns.tsv`. Users name columns and source-system objects at least as often as tables.
3. A chosen relation → `Read relations/<schema>/<relation>.md`. **This step is not optional.** `index.md` carries no column names, so finding your relation there does not equip you to write a query — the page does.
4. A second relation → `Grep joins.tsv` for both addresses **before writing any JOIN**. No row means the join is unverified: say so, and prefer a single-relation answer or ask.
6. "Where does this come from / end up" → `Grep lineage.tsv` for the address. It holds both relation-level and column-level edges.
7. How a value is computed → the `Definition` path on the relation page. Last resort.
8. A domain term that is not a relation or column → `Glob concepts/*.md`.

## Fully-qualified names

Relations are written `{database}.<schema>.<relation>`. Substitute your own
connection's database for `{database}`. The database is never stated by this
pack, because the same shape is deployed to more than one.

Schemas present: bronze, gold, silver.

## Trust

Every fact carries an evidence class. Treat `introspected`, `declared`, `derived`
and `measured` as reliable. Treat `inferred` and `authored` as hypotheses — verify
before relying on them, and say in your answer that they were assumptions.

If `built_at` in `MANIFEST.md` is old, confirm relation and column existence against
the live database before your final query. **Never invent a column**: if it is not in
this pack and not in a live `DESCRIBE`, say so rather than guessing a plausible name.

**The column gate.** Before writing a column name in a query you must have read that
exact name, for that exact relation, in `relations/<schema>/<relation>.md`, in
`columns.tsv`, or from a live `DESCRIBE`. Plausibility is not evidence: a relation
whose description says "diagnosis reference" may well call the column `dx_code` and
not `code`. Reaching for the likely spelling costs a failed query and a turn.

## Reporting obligation

When your answer is a number someone will act on, name the relations you used and
pass through any `EMPTY`, `STALE` or `FUTURE-DATED` marker on them. A freshness caveat this pack
supplied and your answer dropped is the most damaging way this system fails: a
confidently wrong number that nothing downstream catches.
