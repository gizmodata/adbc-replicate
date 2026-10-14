# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Retries on transient connection errors (gRPC "unavailable", such as "the connection is
  draining" when an nginx in front of GizmoSQL reloads). A table load is retried whole
  (`ingest.retries`, default 2, with `ingest.retry_wait_seconds` backoff); that's safe because
  each load is one transaction that replaces the table. A single statement is resent only if it
  failed while being prepared, so it never ran.
- `columns[].rename`: give a column a different target name, for names the target can't take
  (DuckLake reserves `row_id`, `begin_snapshot` and `end_snapshot` while data inlining is on).
  A rename that matches no column fails the plan, like other column rules.
- Oracle `external_tables: include | exclude`. Included external tables are read without
  `AS OF SCN` (files have no SCN), so `consistency: snapshot` no longer fails on them.

### Fixed

- Oracle `BFILE` columns, which the driver can't read, failed the whole table; they're now
  skipped, and the plan names each one.

- View translation no longer produces views that silently return different results. sqlglot,
  by default, warns and then drops what DuckDB lacks: `TO_CHAR(dt, 'YYYY-MM')` became
  `CAST(dt AS TEXT)`, `INSTR`'s occurrence argument was ignored, and `TO_NUMBER(... DEFAULT
  ... ON CONVERSION ERROR)` lost its default. These views are now reported as view errors
  (use an override). Found on a live Oracle schema, where 15 of 322 views were affected.

- Oracle views: `CAST(x AS DATE)` became DuckDB `DATE`, dropping the time of day; it's now
  `TIMESTAMP_S`. `SYSDATE` became UTC "now" although Oracle returns the server's wall-clock
  time (US Eastern on the schema where this was found); it now needs
  `views.source_time_zone`, and the plan warns when that doesn't match the server's clock.
- Oracle `OUTER APPLY` / `CROSS APPLY` produced a lateral join without `ON`, which DuckDB
  rejects; they're now `[LEFT] JOIN LATERAL ... ON TRUE`.

### Changed

- Every dependency floor is now its latest release (pyarrow 26, sqlglot 30.22, pydantic 2.14,
  adbc-driver-manager 1.12, ...): the tool runs in its own environment, and older sqlglot
  releases parse Oracle `SYSTIMESTAMP`, `TRUNC`, `APPLY` and `REGEXP_SUBSTR` differently.
  Compatibility code for older releases is gone.
- Oracle date arithmetic in views follows Oracle: `DATE ± n` is n days later or earlier,
  rounded to the second (a `TIMESTAMP` is truncated to the second first), and `DATE - DATE`
  is a number of days (a DOUBLE, where Oracle's is a 38-digit NUMBER). Operand types come
  from the source catalog, through subqueries and CTEs; compared with Oracle in the
  integration tests.
- Oracle `TRUNC(date)`, `TRUNC(date, 'DD' | 'HH' | 'MM' | 'YYYY' | ...)` and `TO_CHAR` with the
  formats that have an exact DuckDB equivalent are translated; results are compared with
  Oracle's in the integration tests.
- `views.source_pseudo_columns: null` keeps views that select Oracle `ORA_ROWSCN` / `ROWID`
  (APEX optimistic locking), with those columns as NULL; the default still reports them.
- Oracle `TO_NUMBER(x)` (no format) in views becomes `CAST(x AS DECIMAL(38,10))`, exact like
  unconstrained `NUMBER` columns, instead of sqlglot's `DOUBLE`. Optimizer hints are dropped.

## [0.1.0] - 2026-10-14

### Added

- First release: config-driven ADBC-to-ADBC replication into GizmoSQL (or DuckDB).
- Sources: Oracle (adbc-driver-oracle), PostgreSQL, Snowflake and DuckDB, each a small
  plugin for discovery, consistency, change detection, view dialect and type fixes.
- A versioned YAML/JSON config (JSON Schema via `adbc-replicate schema`) with `${VAR}`
  environment references, resolved after YAML parsing; unset variables are an error.
- Selection by case-insensitive glob or `/regex/` on db_schemas, tables and views; excludes;
  per-rule target db_schema templates; views materialized as tables; a copied table
  replaces a same-named view, and references to it inside views are re-pointed.
- Column exclude / allow-list and masks (`null`, salted SHA-256 `hash`, `redact`, source-SQL
  `expression`) evaluated inside the source database. Exclude/include/mask patterns that
  match nothing fail the run (`fail_on_unmatched_column_rules`).
- Consistency modes: `serializable` (one transaction) and `snapshot` (parallel workers at
  one point in time: Oracle `AS OF SCN`, PostgreSQL exported snapshots, Snowflake time travel).
  Snapshot mode is refused at plan time on Snowflake shared (imported) databases, which have
  no time travel.
- Change detection: Snowflake `LAST_ALTERED` for base tables, otherwise a row count plus a
  content fingerprint computed in the source; column-rule changes force a re-copy.
- View translation with sqlglot: Oracle `(+)` joins to ANSI joins, `FROM DUAL`, `ROWNUM`
  limits, Oracle view column lists, dependency-ordered creation, overrides directory and
  `on_failure: materialize`.
- Lossless numerics: unconstrained Oracle `NUMBER` and PostgreSQL `numeric` become
  `DECIMAL(38,10)` (rounded to 10 fractional digits; integer overflow fails loudly).
- Streaming with batch coalescing (65,536 rows / 12 MiB), parallel table copies, atomic
  per-table swaps, `--row-limit` trial runs, `--tables` filters, `plan` and `test-connection`.
- Sync state, run log and per-object results stored in the target (`_adbc_replicate`).
- Emoji text output and `--log-format json`; exit codes 0/1/2/3.
- Container image (`ghcr.io/gizmodata/adbc-replicate`) and a Helm chart running a CronJob.
- Python 3.11 – 3.15.
