# Phrase data model → Query Context Pack

Builds a [Query Context Pack](../../../docs/query-context-pack-spec.md) describing a
Phrase Snowflake database, for an agent that writes queries against it.

Two inputs, and the split between them is the whole design:

- **The live warehouse** (`SNOWFLAKE_*` in `.env`) — authoritative for what exists,
  what its columns are called, their types and nullability.
- **The `snowflake-etl` dbt manifest** (`SNOWFLAKE_ETL_DIR`) — authoritative for what
  things *mean*: descriptions, grain, tags, lineage, and the Clarity crosswalk.

The manifest is never allowed to assert existence, and its `database`/`schema`/
`relation_name` are discarded: it is parsed with one target (`GRAPH_TARGET ?= uvm_gold`),
so those fields record the build, not the relation. Models are bound to introspected
relations by identifier alone. See spec §7.2 and §7.4.

## Run it

```bash
python build.py                      # L2 — no table is scanned
python build.py --freshness          # L3 — one MAX(date) per relation in gold
python build.py --joins --profiles   # L4 — the expensive measurement
python build.py --stage render       # re-render from cached facts, no queries
```

Prerequisites: a `.env` with working `SNOWFLAKE_*` settings (check with
`python scripts/check_snowflake.py`), and a dbt manifest:

```bash
cd $SNOWFLAKE_ETL_DIR/epic/transforms && make parse
```

Output goes to `data/qcp/`, intermediate facts to `data/qcp-work/`.

## Stages

| script | reads | writes | cost |
|---|---|---|---|
| `introspect.py` | `INFORMATION_SCHEMA` | relations, columns, types, row counts | metadata only |
| `transforms.py` | dbt manifest | descriptions, grain, tags, lineage, aliases, sources | local |
| `measure.py freshness` | the data | latest business date per relation | one scan per relation |
| `measure.py joins` | the data | verified join paths + match % | bounded by `--max-pairs` |
| `measure.py profiles` | the data | null rates, distinct counts, top values | expensive |
| `render.py` | the work dir | the pack | none |
| `validate.py` | the pack | conformance report | none |

## Spending guards

Scanning stages skip any relation over `config.MAX_SCAN_BYTES` (5 GB) unless
`--force`. This matters here: `gold.alert_events` is 1.66B rows / 62 GB, and
`gold.ov_summarized_orders_by_prov_day` is 5.76B rows.

`measure.py joins --dry-run` reports the candidate space without issuing a query.
On `gold` today that is 99 key-shaped shared column names → 1,024 raw pairs, pruned
from the 7,918 pairs a naive name match would produce. Pairs are then discarded
unless one side is unique on the key, because a join with no "one" side is a
fan-out, not a join path worth recommending.

## Porting this to another database

`config.py` holds everything Phrase-specific: the repo location, the dbt projects and
their external vocabularies, which schemas get summarised in `index.md`, the join
denylist and the spending budget. The stages themselves are written against the spec.

A different transform tool means replacing `transforms.py` only — it is the one stage
that knows what a dbt manifest looks like. A pack built from `introspect.py` and
`render.py` alone is valid at L0.

## Fixture packs

A pack whose `MANIFEST.md` carries `fixture: true` describes sample data rather than a
warehouse. The database picker offers fixture packs only when `SNOWFLAKE_MODE=mock`
and real packs only when it is `real` — a fixture pack against a live warehouse, or a
real pack against the mock backend, both send the agent looking for relations that are
not there. `workspace/analytics/` is the committed fixture for the offline path.

## Known gaps

- **Column-level lineage is thin.** Only the 56 edges whose dbt column description
  names its own source (`(alert_burden_index.thirty_day_index)`), marked `declared`.
  Real coverage needs `dbt compile` plus a SQL parser; see spec §5.7.
- **No inferred joins.** If measurement has not run, `joins.tsv` is absent rather
  than populated by name matching. The pack's `README.md` tells the consumer so.
- **`SNOWFLAKE_ROLE=PUBLIC`** sees whatever every user in the account sees. A pack
  built under a narrower reporting role will describe fewer relations.
