"""DuckDB-family targets: GizmoSQL (Arrow Flight SQL over DuckDB) and a local DuckDB file.

Both speak DuckDB SQL, so catalog introspection, view DDL and sync-state tables are shared;
only connecting differs. Loads use ADBC bulk ingest (`adbc_ingest`, mode "replace") inside a
transaction, so readers keep seeing the previous copy of a table until the new one commits.
"""

from __future__ import annotations

import time
from contextlib import nullcontext

import pyarrow as pa

from ..config import DuckDBTarget as DuckDBConfig
from ..config import GizmoSQLTarget as GizmoSQLConfig
from ..sources.base import quote_ident

# Network failures while a result streams in from the source (the source driver's message is
# wrapped in the target's ingest error). Snowflake hands results over as chunk downloads from
# cloud storage, and one read timing out fails the whole load: "[Snowflake] arrow/ipc: could not
# read message body: read tcp ...: read: operation timed out".
_NETWORK_ERRORS = ("read: operation timed out", "i/o timeout", "connection reset by peer", "broken pipe",
                   "could not read message body", "unexpected EOF")


def is_transient(error: BaseException | str) -> bool:
    """A connection-level failure worth retrying: gRPC 'unavailable' (a proxy reload sends
    GOAWAY, the server restarts, the network blips) or a network read failing mid-stream, as
    opposed to an error in the request."""
    text = str(error)
    return ("(Unavailable;" in text or "connection is draining" in text or "GOAWAY" in text
            or any(marker in text for marker in _NETWORK_ERRORS))


def _target_unavailable(error: BaseException) -> bool:
    text = str(error)
    return "(Unavailable;" in text or "connection is draining" in text or "GOAWAY" in text


def _failed_before_running(error: BaseException) -> bool:
    """True when a statement failed while being prepared, so it never ran and can be resent."""
    return _target_unavailable(error=error) and "Prepare)" in str(error)


STATEMENT_RETRIES = 3


class DuplicateKeyError(Exception):
    """An incremental merge key isn't unique in the rows being merged."""


class DuckDBFamilyTarget:
    dialect = "duckdb"

    def __init__(self, config) -> None:
        self.config = config
        self.catalog: str | None = config.catalog

    # ---- connections ----------------------------------------------------------------------

    def _open(self):
        raise NotImplementedError

    def connect(self):
        conn = self._open()
        if self.catalog:
            self.execute(conn=conn, sql=f"USE {quote_ident(name=self.catalog)}")
            conn.commit()
        else:
            self.catalog = self.query(conn=conn, sql="SELECT current_database()")[0][0]
            conn.commit()
        return conn

    def worker_connection(self, conn):
        clone = conn.adbc_clone()
        self.execute(conn=clone, sql=f"USE {quote_ident(name=self.catalog)}")
        clone.commit()
        return clone

    # ---- SQL helpers ----------------------------------------------------------------------

    @staticmethod
    def _run(conn, sql: str, parameters: list | None, fetch: bool):
        for attempt in range(STATEMENT_RETRIES + 1):
            try:
                with conn.cursor() as cur:
                    if parameters is None:
                        cur.execute(operation=sql)
                    else:
                        cur.execute(operation=sql, parameters=parameters)
                    return cur.fetchall() if fetch else None
            except Exception as e:
                # Only a statement that never ran is resent; anything later may have executed.
                if attempt == STATEMENT_RETRIES or not _failed_before_running(error=e):
                    raise
                time.sleep(2 ** attempt)

    @staticmethod
    def execute(conn, sql: str, parameters: list | None = None) -> None:
        DuckDBFamilyTarget._run(conn=conn, sql=sql, parameters=parameters, fetch=False)

    @staticmethod
    def query(conn, sql: str, parameters: list | None = None) -> list[tuple]:
        return DuckDBFamilyTarget._run(conn=conn, sql=sql, parameters=parameters, fetch=True)

    def qualified(self, db_schema: str, name: str) -> str:
        parts = [db_schema, name]
        if self.catalog and db_schema.lower() == self.catalog.lower():
            parts.insert(0, self.catalog)   # "shop"."orders" is ambiguous when the catalog is also shop
        return ".".join(quote_ident(name=p) for p in parts)

    # ---- catalog --------------------------------------------------------------------------

    def existing_objects(self, conn) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
        """All (db_schema, name) tables and views in the target catalog — one snapshot, rather
        than a query per object (GizmoSQL v1.39 rejected rapid re-executions of one prepared
        statement with 'Prepared statement is busy')."""
        tables = {(s, n) for s, n in self.query(
            conn=conn, sql="SELECT schema_name, table_name FROM duckdb_tables() WHERE database_name = ?",
            parameters=[self.catalog])}
        views = {(s, n) for s, n in self.query(
            conn=conn, sql="SELECT schema_name, view_name FROM duckdb_views() WHERE database_name = ? AND NOT internal",
            parameters=[self.catalog])}
        conn.commit()
        return tables, views

    def ensure_db_schema(self, conn, db_schema: str) -> None:
        self.execute(conn=conn, sql=f"CREATE SCHEMA IF NOT EXISTS {quote_ident(name=self.catalog)}."
                                    f"{quote_ident(name=db_schema)}")
        conn.commit()

    def drop(self, conn, db_schema: str, name: str, kind: str) -> None:
        self.execute(conn=conn, sql=f"DROP {kind} IF EXISTS {self.qualified(db_schema=db_schema, name=name)}")

    def ingest(self, conn, db_schema: str, table: str, reader: pa.RecordBatchReader, track=None) -> int:
        """Bulk-load `reader` into a new copy of the table. `track(cursor)` (a context manager) lets the
        caller cancel the load from another thread."""
        with conn.cursor() as cur, (track(cursor=cur) if track else nullcontext()):
            rows = cur.adbc_ingest(table_name=table, data=reader, mode="replace",
                                   catalog_name=self.catalog, db_schema_name=db_schema)
        return rows

    def merge(self, conn, db_schema: str, table: str, key: list[str], staging_db_schema: str, changes: str,
              keys: str | None) -> tuple[int, int]:
        """MERGE the staged changed rows into the table on `key` (update matches, insert the rest, both
        by column name), and when `keys` (a staged list of every source key) is given, MERGE away the
        target rows whose key is gone. Runs in the caller's transaction; drops the staging tables.
        DuckDB's MERGE accepts several source rows with one key and applies an arbitrary one, so
        staged changes with a repeated key raise DuplicateKeyError instead (the key isn't unique;
        the caller copies the table whole). Returns (rows merged, rows deleted)."""
        target = self.qualified(db_schema=db_schema, name=table)
        staged = self.qualified(db_schema=staging_db_schema, name=changes)
        columns = ", ".join(quote_ident(name=k) for k in key)
        repeated = self.query(conn=conn, sql=f"SELECT count(*) FROM (SELECT {columns} FROM {staged} "
                                             f"GROUP BY ALL HAVING count(*) > 1)")[0][0]
        if repeated:
            raise DuplicateKeyError(f"{repeated} key value(s) of ({', '.join(key)}) occur more than once among "
                                    "the changed rows, so the key isn't unique")
        match = " AND ".join(f"t.{quote_ident(name=k)} = s.{quote_ident(name=k)}" for k in key)
        merged = self.update(conn=conn, sql=f"MERGE INTO {target} AS t USING {staged} AS s ON ({match}) "
                                            "WHEN MATCHED THEN UPDATE BY NAME WHEN NOT MATCHED THEN INSERT BY NAME")
        deleted = 0
        if keys is not None:
            current = self.qualified(db_schema=staging_db_schema, name=keys)
            deleted = self.update(conn=conn, sql=f"MERGE INTO {target} AS t USING {current} AS s ON ({match}) "
                                                 "WHEN NOT MATCHED BY SOURCE THEN DELETE")
            self.execute(conn=conn, sql=f"DROP TABLE {current}")
        self.execute(conn=conn, sql=f"DROP TABLE {staged}")
        return merged, deleted

    def append(self, conn, db_schema: str, table: str, staging_db_schema: str, changes: str) -> int:
        """INSERT the staged rows into the table by column name (refresh.append); drops the staging table."""
        target = self.qualified(db_schema=db_schema, name=table)
        staged = self.qualified(db_schema=staging_db_schema, name=changes)
        inserted = self.update(conn=conn, sql=f"INSERT INTO {target} BY NAME SELECT * FROM {staged}")
        self.execute(conn=conn, sql=f"DROP TABLE {staged}")
        return inserted

    @staticmethod
    def update(conn, sql: str) -> int:
        """Run a DML statement and return the number of rows it affected."""
        with conn.cursor() as cur:
            cur.execute(operation=sql)
            return max(cur.rowcount, 0)

    def checkpoint(self, conn) -> str | None:
        """Fold the WAL into the database file. Returns an error message instead of raising."""
        try:
            self.execute(conn=conn, sql="CHECKPOINT")
            conn.commit()
            return None
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return str(e)


class GizmoSQLTarget(DuckDBFamilyTarget):
    config: GizmoSQLConfig

    def _open(self):
        import adbc_driver_gizmosql.dbapi as gizmosql

        db_kwargs = {"adbc.flight.sql.client_option.with_max_msg_size": str(self.config.max_msg_size),
                     **self.config.options}
        return gizmosql.connect(uri=self.config.uri, username=self.config.username, password=self.config.password,
                                tls_skip_verify=self.config.tls_skip_verify, db_kwargs=db_kwargs, autocommit=False)


class DuckDBTarget(DuckDBFamilyTarget):
    config: DuckDBConfig

    def _open(self):
        from ..sources.duckdb import duckdb_connect

        return duckdb_connect(path=self.config.path)


def make_target(config) -> DuckDBFamilyTarget:
    if config.type == "gizmosql":
        return GizmoSQLTarget(config=config)
    if config.type == "duckdb":
        return DuckDBTarget(config=config)
    raise ValueError(f"unknown target type {config.type!r}")
