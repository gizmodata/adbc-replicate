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
  `all` extra skips it there). Dependency floors are the latest releases (the tool gets its own
  venv/container): raise them when bumping rather than adding compatibility code. The
  lowest-dependencies CI job installs exactly the floors.
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
  with every new rewrite, with NULL, decimal and date inputs.
- sqlglot also passes Oracle `||` through, and DuckDB's `||` is NULL if either side is (Oracle: '');
  Oracle's '' is NULL, so string functions' empty results must become NULL; numbers and dates
  turn into text Oracle's way (`_oracle_text`). DuckDB's multi-column UNPIVOT drops a row when any
  value is NULL (Oracle: all), so it's rewritten as UNION ALL.
- Oracle `GREATEST`/`LEAST` are NULL if any argument is; DuckDB's skip NULLs (sqlglot even marks Oracle's
  `ignore_nulls=True`), so they're wrapped in an all-arguments-present CASE. `TO_DATE(TO_CHAR(date), fmt)`
  reads the DD-MON-RR text leniently by field position (`'YYYY-MM-DD'` gives year 16): `_to_date_of_nls_text`.
- Oracle's `''` is NULL: source `''` literals become `CAST(NULL AS VARCHAR)` as values, comparison operands and
  function arguments, except where Oracle reads NULL as `''` (REPLACE, REGEXP_REPLACE, `||`) — done on the
  untouched tree, so the translator's own `''` (nullif templates) is never touched.
- Names: Oracle/Snowflake fold unquoted names to upper case and quoted ones (`"PartNo"`) keep theirs; DuckDB
  ignores case. A reference that matches a column only case-insensitively is resolved by the source in an outer
  query, so `_case_sensitive_references` refuses it (planner passes exact names in `Plan.column_names`).
- DuckDB 1.5.6 `strftime()` on a `TIMESTAMP_S` before 1677 fails ("Could not convert Timestamp(US) to
  Timestamp(NS)"); cast to `TIMESTAMP` first when formatting dates that may be that old.
- An audit that runs heavy views on a pico GizmoSQL cluster (1 GB, `memory_limit` 1 GB) can OOM-kill the
  server: DuckDB's RSS goes well past `memory_limit`. Reproduce heavy views locally (copy the tables to a DuckDB
  file, `threads=1`, same `memory_limit`, `/usr/bin/time -l`) instead of on a customer's cluster.
- GizmoSQL behind nginx: long DuckDB error texts (DuckLake commit failures, view binder errors
  quoting the whole statement) arrive as `RST_STREAM INTERNAL_ERROR` instead of the message.
  Reproduce against a local DuckDB file target (`--row-limit 0`, schema only) to read them.
- The Snowflake CI user (`ADBC_REPLICATE_CI`) has only PUBLIC: it can read the sample share and
  use SNOWFLAKE_LEARNING_WH, nothing else. Snowflake tests must stay read-only.
