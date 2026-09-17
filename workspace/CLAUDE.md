# Reporting workspace

<!--
  Replace this file with the contents of your local AGENTS.md.
  Claude Code loads CLAUDE.md (not AGENTS.md); if you want to keep the AGENTS.md
  name, make this file a single line:  @AGENTS.md
-->

You produce analytical reports from Snowflake for Phrase Health users.

## Data access

- Query Snowflake only through the `run_sql`, `list_tables` and `describe_table` tools.
  They are read-only and cap results at a few hundred rows: aggregate in SQL.
- The `transforms/` directory is the dbt/transform repository. Its models define
  every table you may use. Before writing SQL, look up the model (`Glob transforms/**/*.sql`,
  `Read` the model and its `.yml` schema) so column names and grain are right.
- Never guess a column. If a table is not in `transforms/`, say so.

## Report structure

1. Title (concise, specific — "Sepsis Screen acceptance, Aug 2026", not "Report").
2. Summary: 3–5 sentences with the headline numbers.
3. Findings: one `##` section per question, each with a table or a short list of figures.
4. Method: tables used, filters, date range, caveats.

## Style

- Plain, direct prose. No filler.
- Numbers: thousands separators, percentages to one decimal, dates as YYYY-MM-DD.
- Markdown tables for anything with more than three values.

## Publishing

When the user confirms the content, call `publish_report(title, body_markdown)` once.
Do not include the title inside `body_markdown`. Then tell the user it is ready.
