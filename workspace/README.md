# Data notes for the agent

Hand-written notes that the generated pack cannot supply: metric definitions your
team has agreed, known data-quality traps, house conventions.

Everything structural — what relations exist, their columns and types, descriptions,
lineage, the Clarity crosswalk, row counts and freshness — lives in `qcp/` and is
regenerated from the warehouse and the `snowflake-etl` dbt manifest. Do not
duplicate it here; it will drift.

Query strategy that is worth writing down ("a referral is orders joined to
encounters filtered by …") belongs in `qcp/concepts/<slug>.md`, which survives pack
rebuilds. Each concept file must declare an owner and a review date.
