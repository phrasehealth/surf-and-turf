# cds.alert_daily

`{database}.cds.alert_daily` · base table

Daily alert totals: firings and acceptance rate per alert.

## Columns

| column | type | null | description |
|---|---|---|---|
| `day` | DATE | YES | |
| `alert_name` | VARCHAR | YES | |
| `firings` | NUMBER | YES | |
| `accept_rate` | FLOAT | YES | |
