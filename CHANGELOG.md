# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
