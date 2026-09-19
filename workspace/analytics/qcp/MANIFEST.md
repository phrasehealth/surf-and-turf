# Query Context Pack — analytics

qcp_version: 1.0
pack_name: analytics
built_at: 2026-09-19T12:55:24Z
database_placeholder: {database}
schemas: cds
relations: 2
columns: 9
conformance: L0
index_schemas: cds
evidence_present: introspected
sources:
  - fixtures: app/tools/snowflake_sql.py MockBackend
coverage:
  descriptions: 2/2 relations, 0/9 columns
note: |
  Sample data for AGENT_MODE=mock / SNOWFLAKE_MODE=mock. Committed on purpose so the
  offline path exercises the same pack lookup as a real database rather than skipping it.
