"""Sync state kept in the target itself: per-table fingerprints, per-view definition hashes, a
run log and per-object results. Keeping it next to the data means a fresh container (or a
different machine) picks up where the last run left off, and a UI can read run history with
plain SQL."""

from __future__ import annotations

import datetime
import json

from .sources.base import quote_ident

RUN_STATUSES = ("running", "completed", "completed_with_view_errors", "failed", "abandoned")


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


def to_utc(ts: datetime.datetime | None) -> datetime.datetime | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.UTC)
    return ts.astimezone(datetime.UTC)


class StateStore:
    def __init__(self, target, conn, db_schema: str) -> None:
        self.target = target
        self.conn = conn
        self.s = quote_ident(name=db_schema)
        self.db_schema = db_schema

    def _exec(self, sql: str, parameters: list | None = None) -> None:
        self.target.execute(conn=self.conn, sql=sql, parameters=parameters)

    def _query(self, sql: str, parameters: list | None = None) -> list[tuple]:
        return self.target.query(conn=self.conn, sql=sql, parameters=parameters)

    def ensure(self) -> None:
        s = self.s
        self._exec(f"CREATE SCHEMA IF NOT EXISTS {s}")
        self._exec(f"""
            CREATE TABLE IF NOT EXISTS {s}.table_state (
                db_schema_name    VARCHAR NOT NULL,
                table_name        VARCHAR NOT NULL,
                source_catalog    VARCHAR,
                source_db_schema  VARCHAR NOT NULL,
                source_table      VARCHAR NOT NULL,
                source_kind       VARCHAR NOT NULL,
                change_marker     TIMESTAMPTZ,
                fingerprint       VARCHAR,
                row_count         BIGINT,
                column_signature  VARCHAR,
                last_copied_at    TIMESTAMPTZ,
                last_checked_at   TIMESTAMPTZ,
                last_run_id       VARCHAR,
                change_token      VARCHAR
            )""")
        # added in 0.3 (incremental loads): state tables from older versions get the column
        self._exec(f"ALTER TABLE {s}.table_state ADD COLUMN IF NOT EXISTS change_token VARCHAR")
        self._exec(f"""
            CREATE TABLE IF NOT EXISTS {s}.view_state (
                db_schema_name    VARCHAR NOT NULL,
                view_name         VARCHAR NOT NULL,
                definition_hash   VARCHAR NOT NULL,
                last_created_at   TIMESTAMPTZ,
                last_run_id       VARCHAR
            )""")
        self._exec(f"""
            CREATE TABLE IF NOT EXISTS {s}.run_log (
                run_id            VARCHAR NOT NULL,
                replication       VARCHAR,
                tool_version      VARCHAR,
                started_at        TIMESTAMPTZ NOT NULL,
                finished_at       TIMESTAMPTZ,
                status            VARCHAR NOT NULL,
                refresh_mode      VARCHAR,
                row_limit         BIGINT,
                snapshot          VARCHAR,
                tables_total      INTEGER,
                tables_copied     INTEGER,
                tables_skipped    INTEGER,
                tables_failed     INTEGER,
                rows_copied       BIGINT,
                views_total       INTEGER,
                views_created     INTEGER,
                views_skipped     INTEGER,
                views_failed      INTEGER,
                error             VARCHAR
            )""")
        self._exec(f"""
            CREATE TABLE IF NOT EXISTS {s}.run_objects (
                run_id            VARCHAR NOT NULL,
                object_kind       VARCHAR NOT NULL,
                source_name       VARCHAR NOT NULL,
                db_schema_name    VARCHAR NOT NULL,
                object_name       VARCHAR NOT NULL,
                action            VARCHAR NOT NULL,
                reason            VARCHAR,
                rows_copied       BIGINT,
                seconds           DOUBLE,
                error             VARCHAR,
                finished_at       TIMESTAMPTZ
            )""")
        self.conn.commit()

    # ---- tables ---------------------------------------------------------------------------

    def load_tables(self) -> dict[tuple[str, str], dict]:
        rows = self._query(f"SELECT db_schema_name, table_name, change_marker, fingerprint, row_count, "
                           f"column_signature, change_token FROM {self.s}.table_state")
        self.conn.commit()
        return {(r[0], r[1]): {"change_marker": to_utc(ts=r[2]), "fingerprint": r[3], "row_count": r[4],
                               "column_signature": r[5], "change_token": r[6]} for r in rows}

    def save_table(self, task, change_marker, fingerprint: str | None, row_count: int | None,
                   column_signature: str, copied: bool, run_id: str, change_token: str | None = None) -> None:
        """Upsert one table's state (DELETE + INSERT: no key index needed on the state table)."""
        db_schema, table = task.target_key
        now = utc_now()
        previous = self._query(f"SELECT last_copied_at FROM {self.s}.table_state "
                               f"WHERE db_schema_name = ? AND table_name = ?", [db_schema, table])
        last_copied_at = now if copied else (to_utc(ts=previous[0][0]) if previous else None)
        self._exec(f"DELETE FROM {self.s}.table_state WHERE db_schema_name = ? AND table_name = ?",
                   [db_schema, table])
        self._exec(f"INSERT INTO {self.s}.table_state (db_schema_name, table_name, source_catalog, "
                   "source_db_schema, source_table, source_kind, change_marker, fingerprint, row_count, "
                   "column_signature, last_copied_at, last_checked_at, last_run_id, change_token) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   [db_schema, table, task.source.catalog, task.source.db_schema, task.source.name,
                    task.source.kind, to_utc(ts=change_marker), fingerprint, row_count, column_signature,
                    last_copied_at, now, run_id, change_token])
        self.conn.commit()

    def forget_table(self, db_schema: str, table: str) -> None:
        """Drop a table's state, so the next run copies it in full (after a --row-limit sample)."""
        self._exec(f"DELETE FROM {self.s}.table_state WHERE db_schema_name = ? AND table_name = ?",
                   [db_schema, table])
        self.conn.commit()

    # ---- views ----------------------------------------------------------------------------

    def load_views(self) -> dict[tuple[str, str], str]:
        rows = self._query(f"SELECT db_schema_name, view_name, definition_hash FROM {self.s}.view_state")
        self.conn.commit()
        return {(r[0], r[1]): r[2] for r in rows}

    def forget_view(self, db_schema: str, view: str) -> None:
        self._exec(f"DELETE FROM {self.s}.view_state WHERE db_schema_name = ? AND view_name = ?", [db_schema, view])
        self.conn.commit()

    def save_view(self, db_schema: str, view: str, definition_hash: str, run_id: str) -> None:
        self._exec(f"DELETE FROM {self.s}.view_state WHERE db_schema_name = ? AND view_name = ?", [db_schema, view])
        self._exec(f"INSERT INTO {self.s}.view_state VALUES (?, ?, ?, ?, ?)",
                   [db_schema, view, definition_hash, utc_now(), run_id])
        self.conn.commit()

    # ---- runs -----------------------------------------------------------------------------

    def start_run(self, run_id: str, replication: str | None, tool_version: str, refresh_mode: str,
                  row_limit: int | None) -> None:
        # Runs on one target must not overlap (schedule them with concurrencyPolicy: Forbid), so
        # a row still 'running' belongs to a process that died before recording its result.
        self._exec(f"UPDATE {self.s}.run_log SET status = 'abandoned', finished_at = ?, "
                   f"error = 'no result recorded; the process was probably killed' WHERE status = 'running'",
                   [utc_now()])
        self._exec(f"INSERT INTO {self.s}.run_log (run_id, replication, tool_version, started_at, status, "
                   f"refresh_mode, row_limit) VALUES (?, ?, ?, ?, 'running', ?, ?)",
                   [run_id, replication, tool_version, utc_now(), refresh_mode, row_limit])
        self.conn.commit()

    def finish_run(self, run_id: str, status: str, stats: dict, snapshot: str | None, error: str | None) -> None:
        self._exec(f"""
            UPDATE {self.s}.run_log
            SET finished_at = ?, status = ?, snapshot = ?, tables_total = ?, tables_copied = ?,
                tables_skipped = ?, tables_failed = ?, rows_copied = ?, views_total = ?, views_created = ?,
                views_skipped = ?, views_failed = ?, error = ?
            WHERE run_id = ?""",
                   [utc_now(), status, snapshot, stats.get("tables_total"), stats.get("tables_copied"),
                    stats.get("tables_skipped"), stats.get("tables_failed"), stats.get("rows_copied"),
                    stats.get("views_total"), stats.get("views_created"), stats.get("views_skipped"),
                    stats.get("views_failed"), _clip(text=error), run_id])
        self.conn.commit()

    def record_object(self, run_id: str, kind: str, source_name: str, db_schema: str, name: str, action: str,
                      reason: str | None = None, rows: int | None = None, seconds: float | None = None,
                      error: str | None = None) -> None:
        self._exec(f"INSERT INTO {self.s}.run_objects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   [run_id, kind, source_name, db_schema, name, action, reason, rows, seconds,
                    _clip(text=error), utc_now()])
        self.conn.commit()


def _clip(text: str | None, limit: int = 4000) -> str | None:
    """Shorten an error for storage keeping its head AND tail: the cause is usually at the end."""
    if text is None or len(text) <= limit:
        return text
    half = (limit - 20) // 2
    return f"{text[:half]}\n... [clipped] ...\n{text[-half:]}"


def column_signature(entries: list) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(entries, sort_keys=True, default=str).encode()).hexdigest()[:32]
