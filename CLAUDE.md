# CLAUDE.md

Project notes for Claude and other contributors.

## What this is

`adbc-replicate`: config-driven ADBC → ADBC replication into GizmoSQL (or DuckDB). Python;
the data path is Arrow C streams between drivers (`fetch_record_batch` → `adbc_ingest`), so
Python never touches rows. It generalizes per-customer jobs like
the Snowflake → GizmoSQL loader behind the Vaden Automotive case study (its lessons are
what the defaults here encode: 12 MiB batch coalescing, 32 MiB gRPC cap, one catalog snapshot
instead of per-object queries, `abandoned` runs). Planned later: a GizmoData Cloud wizard
that writes this config, so keep the config schema, `plan --format json` and the event
stream stable and UI-friendly.

## Layout

```
src/adbc_replicate/config.py      pydantic models = the config contract (+ JSON Schema)
src/adbc_replicate/planner.py     rules + source catalog -> Plan (tables, views, name_map)
src/adbc_replicate/engine.py      run: consistency, parallel copies, change detection, views
src/adbc_replicate/sources/*.py   one plugin per database (base.py is the interface)
src/adbc_replicate/targets/       DuckDB-family targets (GizmoSQL, DuckDB)
src/adbc_replicate/views.py       sqlglot translation (Oracle rewrites, name re-pointing)
src/adbc_replicate/state.py       sync state / run log tables in the target
tests/unit                        no servers; DuckDB -> DuckDB engine tests, README YAML check
tests/integration                 real GizmoSQL + Oracle + PostgreSQL (docker-compose.yml);
                                  Snowflake tests are marker `snowflake` (need credentials)
charts/adbc-replicate             Helm CronJob
```

## Conventions

- ADBC vocabulary everywhere (catalog, db_schema, table) — never Oracle-specific terms.
- Keyword arguments at call sites (also for pyarrow/ADBC calls that accept them).
- Python floor 3.11; CI matrix 3.11–3.14 + a 3.15 job (DuckDB has no 3.15 wheels yet, so the
  `all` extra skips it there). The lowest-dependencies CI job keeps pyproject floors honest.
- Tests read only `ADBC_REPLICATE_TEST_*` variables: developer shells export real
  `GIZMOSQL_*` credentials. Docker addresses are `127.0.0.1`, never `localhost`.
- Integration tests FAIL (never skip) when a server is unreachable.
- Every YAML block in README.md is validated by tests/unit/test_readme.py; quick-start
  output in the README was captured from a real run — re-capture it if output changes.
- Releases: bump `src/adbc_replicate/__init__.py`, `charts/adbc-replicate/Chart.yaml`
  (version + appVersion) and CHANGELOG together; tag `vX.Y.Z`. release.yml re-runs ci.yml on
  the tag and checks all three versions match before publishing to PyPI, GHCR (image + chart)
  and creating the GitHub Release.

## Gotchas found the hard way

- Oracle `AS OF SCN` fails with ORA-01466 for ~2 s after a table is created/altered/truncated.
  `TIMESTAMP_TO_SCN` is ~3 s coarse; an exact SCN needs `EXECUTE ON SYS.DBMS_FLASHBACK`
  (only SYS can grant it in 23ai). The integration fixture waits until flashback reads work.
- Oracle unconstrained `NUMBER` reads as float64 by default; we CAST to NUMBER(38,10).
  Oracle computed columns (CASE, functions) lose precision metadata — but
  `CASE WHEN 1 = 0 THEN col END` keeps it, which is how the `null` mask stays typed.
- PostgreSQL's driver returns `numeric` as text (arrow.opaque); we round in SQL then strictly
  cast to decimal128 client-side. `pg_get_viewdef` expands `SELECT *` into explicit columns.
- sqlglot silently drops Oracle `(+)` when generating; always run `eliminate_join_marks` first.
  sqlglot renamed `Select.args["from"]` to `"from_"` — use the `_FROM` constant in views.py.
- DuckDB can't resolve `"x"."t"` when a catalog and a db_schema are both named `x`; target
  DDL and translated views qualify with the catalog exactly in that case.
- DuckDB's ADBC driver supports ingest mode `replace` only from 1.5.x (floor: 1.5.6).
- The GizmoSQL driver's wheels need glibc >= 2.34 (Docker base: bookworm or newer).
- Snowflake shared (imported) databases have no time travel: `AT(TIMESTAMP => …)` fails with
  000707 on every table (found on SNOWFLAKE_SAMPLE_DATA), so the plan refuses snapshot mode
  there (`INFORMATION_SCHEMA.DATABASES.TYPE = 'IMPORTED DATABASE'`).
- sqlglot (Oracle → DuckDB) silently: drops unsupported arguments unless generated with
  `unsupported_level=RAISE`; maps `CAST(x AS DATE)` to DuckDB `DATE` (Oracle DATE has a time:
  use `TIMESTAMP_S`); maps `SYSDATE` to UTC (it's the server's wall clock — often not UTC, e.g. US
  Eastern); passes Oracle `TRUNC` units ('DD') and unknown functions through unchanged (those
  fail loudly in DuckDB). views.py rewrites or refuses each; rewrites of Oracle functions are
  compared with real Oracle in `tests/integration/test_oracle_view_functions.py` — extend it
  with every new rewrite.
- GizmoSQL behind nginx: long DuckDB error texts (DuckLake commit failures, view binder errors
  quoting the whole statement) arrive as `RST_STREAM INTERNAL_ERROR` instead of the message.
  Reproduce against a local DuckDB file target (`--row-limit 0`, schema only) to read them.
- The Snowflake CI user (`ADBC_REPLICATE_CI`) has only PUBLIC: it can read the sample share and
  use SNOWFLAKE_LEARNING_WH, nothing else. Snowflake tests must stay read-only.
