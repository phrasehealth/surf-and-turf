# cds.alert_firings

`{database}.cds.alert_firings` · base table

One row per alert firing, with the facility and whether it was accepted.

## Columns

| column | type | null | description |
|---|---|---|---|
| `alert_id` | VARCHAR | YES | |
| `alert_name` | VARCHAR | YES | |
| `fired_at` | TIMESTAMP_NTZ | YES | |
| `facility` | VARCHAR | YES | |
| `accepted` | BOOLEAN | YES | |
