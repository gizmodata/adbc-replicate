# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-10-15

### Added

- Oracle hierarchical queries in views (`START WITH` / `CONNECT BY [NOCYCLE]`, `PRIOR`, `LEVEL`,
  `SYS_CONNECT_BY_PATH`, `CONNECT_BY_ROOT`, `CONNECT_BY_ISLEAF`, `ORDER SIBLINGS BY`, `ROWNUM` as
  the depth-first row number, `START WITH ... AND ROWNUM = 1`, `CONNECT BY LEVEL <= n`) are
  translated to `WITH RECURSIVE`. Cycles stop where Oracle's NOCYCLE stops (a row repeating an
  ancestor's PRIOR values) instead of recursing forever.
- `MIN/MAX(x) KEEP (DENSE_RANK FIRST/LAST ORDER BY ...)`, aggregate and analytic, becomes
  `min_by`/`max_by` over a `create_sort_key` of the sort keys plus `x` (ties take the MIN/MAX of
  `x`, as in Oracle; any ASC/DESC/NULLS order).
- `INSTR(s, sub, position, occurrence)` is translated exactly: Nth occurrence, overlapping
  matches, backward search from a negative position, NULLs.
- `SYS_CONTEXT(...)` reads as NULL with `views.source_pseudo_columns: "null"` (what Oracle returns
  when the context isn't set), and `TO_DATE`/`TO_TIMESTAMP` of NULL is a typed NULL.
- `views.functions`: rewrite your own source functions (e.g. PL/SQL) as DuckDB SQL templates,
  with `{1}`, `{2}`, ... for positional and `{p_name}` for named arguments. A key can list the
  parameters, `NAME(p1, p2)`, so positional and named calls both work, and declare `RETURN DATE`
  (or NUMBER, VARCHAR2) so date arithmetic and text conversions on the result are translated.
- `views.sys_context_macros`: a parameterized Oracle view (reading `SYS_CONTEXT`) also gets a DuckDB
  table macro of the same name, one parameter per context attribute (default NULL):
  `SELECT * FROM v(attribute := value)`.
- More Oracle translations: multi-column `UNPIVOT` (as `UNION ALL`; DuckDB's own drops a row when
  any value is NULL, Oracle's only when all are), recursive `WITH` without the `RECURSIVE` keyword,
  `x op ALL/ANY (list)`, `ROWNUM = 1`, `FROM_TZ`, `NUMTODSINTERVAL`, `STANDARD_HASH`/`RAWTOHEX`,
  `TO_CHAR(n, '000')`, `TO_DATE(SYSDATE)`, `OUTER APPLY (cte)` and `DUAL` in joins; columns renamed by
  column rules are re-pointed in views. Type inference follows `WITH c (a, b) AS (...)` column lists.
- Each of these is checked row by row against Oracle in the integration tests. On a real
  303-view Oracle schema, 73 more views now replicate (217 -> 290, of 296 not excluded).

### Changed

- Oracle `LONG` and `LONG RAW` columns are skipped (and listed in the plan), like `BFILE`.

### Fixed

- Oracle `||` and `CONCAT` were passed through to DuckDB, whose `||` returns NULL when either side
  is NULL (Oracle treats NULL as ''), so any translated view concatenating a nullable column could
  return NULL where Oracle returns text; numbers and dates also turned into DuckDB's text
  (`1.5000000000`, `2026-03-08 09:05:07`) instead of Oracle's (`1.5`, `08-MAR-26`). Now
  `NULLIF(CONCAT(...), '')` with Oracle's conversions. `TO_CHAR(x)`, `SUBSTR` (position 0,
  fractions, negative positions, lengths below 1), `TRIM`/`LPAD`/`REPLACE`/... of numbers, and their
  empty results (NULL in Oracle) follow Oracle too.
- Oracle `TIMESTAMP '...'` literals became `STRPTIME(..., '%Y-%m-%d %H:%M:%S.%f')`, which fails when
  the view is queried (not when it's created) unless the literal has a fraction; now a cast, with
  the zone offset kept.
- `INSTR(s, sub, -1)` (the last occurrence) was translated by sqlglot as a check of the last
  character only, silently returning wrong positions; and `INSTR(s, sub, 1, 1)` was refused.

## [0.3.1] - 2026-10-14

### Fixed

- A view that couldn't be recreated (e.g. after a column it reads was newly excluded) kept its
  previous version in the target, and later runs counted it "unchanged" by definition hash, so
  it stayed broken without being reported. Now a view must also still bind to count as
  unchanged, and a view that can't be recreated is dropped from the target (and reported).
  Found on a real 303-view schema, where 19 such views were left behind.

## [0.3.0] - 2026-10-14

### Added

- Incremental loads: `refresh.mode: incremental` merges only the rows changed since the last run
  into each table with a primary key (DuckDB `MERGE INTO ... UPDATE BY NAME / INSERT BY NAME`), and
  `refresh.detect_deletes` (default on) removes rows whose key is gone. Primary keys come from the
  driver's ADBC `GetObjects` metadata. Oracle finds changed rows with `ORA_ROWSCN` against the
  previous run's snapshot SCN (commit-ordered, so late commits and RAC sequence caches can't
  cause misses). Tables without a usable key, and sources without change tracking, keep
  `changed`-mode behavior; `plan` says which is which.

### Fixed

- Stopping a run (SIGINT, or the SIGTERM Kubernetes sends when it evicts a pod) cancelled at most
  the main thread's statement, or none: killed mid-load, the process left GizmoSQL sessions
  holding open transactions, and the next run's replace of those tables failed with "Catalog
  write-write conflict". Now every in-flight statement in every worker is cancelled, the run is
  logged as `interrupted` (exit 130/143) and notifications fire.

## [0.2.0] - 2026-10-14

### Added

- Webhook notifications when a run finishes: `notifications:` with a Slack Block Kit message
  or a JSON run summary, chosen per webhook by outcome (`when: [failure, view_errors,
  success]`). Runs that fail before or while connecting notify too; an unreachable webhook is
  a warning, never a failed run; URLs are never printed. `adbc-replicate test-notifications`
  sends a test message.

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
