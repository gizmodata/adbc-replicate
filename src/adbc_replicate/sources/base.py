"""The source-plugin interface: everything ADBC doesn't standardise.

ADBC gives us connections and Arrow streams. What differs per database is how to list
db_schemas, tables, views and columns; how to read a consistent snapshot; how to fingerprint a
table's content cheaply; which SQL dialect views are written in; and a few type conversions.
Each plugin answers those questions and nothing else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pyarrow as pa

from ..config import MaskSpec
from ..models import Column, SourceObject, TableTask


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def in_list(values: list[str]) -> str:
    return ", ".join(quote_literal(value=v) for v in values) or "NULL"


@dataclass
class Snapshot:
    """A point in time every worker reads at (consistency: snapshot)."""
    token: str          # SCN, exported snapshot id, timestamp, ...
    description: str


class Source(ABC):
    #: sqlglot dialect the source writes view definitions in
    dialect: str = ""
    #: True when one connection can read other catalogs by qualifying names
    cross_catalog: bool = False

    def __init__(self, config) -> None:
        self.config = config
        self.snapshot: Snapshot | None = None

    # ---- connections ----------------------------------------------------------------------

    @abstractmethod
    def connect(self):
        """Open a DBAPI connection (autocommit off unless the plugin manages transactions itself)."""

    def worker_connection(self, conn):
        """A further connection for a parallel worker, sharing the driver's database handle."""
        clone = conn.adbc_clone()
        if self.snapshot is not None:
            self.join_snapshot(conn=clone, snapshot=self.snapshot)
        return clone

    def prepare_cursor(self, cursor) -> None:
        """Set statement options (result queue sizes, ...) on a fresh cursor."""

    # ---- consistency ----------------------------------------------------------------------

    def begin_serializable(self, conn) -> None:
        raise NotImplementedError(f"consistency 'serializable' isn't supported for {self.config.type}")

    def begin_snapshot(self, conn) -> Snapshot:
        raise NotImplementedError(f"consistency 'snapshot' isn't supported for {self.config.type}")

    def join_snapshot(self, conn, snapshot: Snapshot) -> None:
        """Make a worker connection read at `snapshot`. Default: nothing to do, because the
        snapshot is applied in every query's table reference."""

    # ---- discovery ------------------------------------------------------------------------

    def current_catalog(self, conn) -> str | None:
        return None

    def check_catalog(self, conn, catalog: str | None) -> None:
        if catalog is None or self.cross_catalog:
            return
        current = self.current_catalog(conn=conn)
        if current is None or current.upper() != catalog.upper():
            raise ValueError(f"{self.config.type} reads only the connection's own catalog ({current}); "
                             f"connect to {catalog!r} instead of naming it in objects[].catalog")

    @abstractmethod
    def list_db_schemas(self, conn, catalog: str | None) -> list[str]:
        ...

    @abstractmethod
    def list_objects(self, conn, catalog: str | None, db_schemas: list[str]) -> list[SourceObject]:
        """Tables, views and materialized views in `db_schemas`, with their columns in order."""

    @abstractmethod
    def view_definitions(self, conn, catalog: str | None, views: list[SourceObject]) -> dict[tuple[str, str], str]:
        """{(db_schema, view name): definition} — a bare SELECT or a full CREATE VIEW."""

    # ---- query building -------------------------------------------------------------------

    def quote(self, name: str) -> str:
        return quote_ident(name=name)

    def table_ref(self, obj: SourceObject) -> str:
        parts = [obj.db_schema, obj.name]
        if obj.catalog and self.cross_catalog:
            parts.insert(0, obj.catalog)
        return ".".join(self.quote(name=p) for p in parts)

    @abstractmethod
    def text_expr(self, expr: str) -> str:
        """SQL converting any value to text."""

    @abstractmethod
    def sha256_hex_expr(self, expr: str) -> str:
        """SQL computing the lower-case hex SHA-256 of a text expression."""

    def native_expr(self, column: Column) -> str:
        """The column as selected when unmasked (plugins add lossless casts here)."""
        return self.quote(name=column.name)

    def lossy_type(self, column: Column) -> str | None:
        """Why this column's values would change on the way to the target when copied unmasked (the plan
        refuses it), or None. For types the driver hands over in a form that silently loses data."""
        return None

    def column_expr(self, column: Column, mask: MaskSpec | None) -> str:
        col = self.quote(name=column.name)
        if mask is None:
            expr = self.native_expr(column=column)
        elif mask.strategy == "null":
            expr = f"CASE WHEN 1 = 0 THEN {col} END"          # keeps the column's type
        elif mask.strategy == "redact":
            expr = f"CASE WHEN {col} IS NULL THEN NULL ELSE {quote_literal(value=mask.value)} END"
        elif mask.strategy == "hash":
            salted = self.text_expr(expr=col)
            if mask.salt:
                salted = f"{quote_literal(value=mask.salt)} || {salted}"
            expr = f"CASE WHEN {col} IS NULL THEN NULL ELSE {self.sha256_hex_expr(expr=salted)} END"
        else:
            expr = mask.sql.replace("{column}", col)
        return f"{expr} AS {col}"

    def limit_clause(self, row_limit: int) -> str:
        return f" LIMIT {int(row_limit)}"

    def select_sql(self, task: TableTask, row_limit: int | None = None, where: str | None = None) -> str:
        exprs = ", ".join(self.column_expr(column=c.column, mask=c.mask) for c in task.columns)
        sql = f"SELECT {exprs} FROM {self.table_ref(obj=task.source)}"
        if where:
            sql += f" WHERE {where}"
        if row_limit is not None:
            sql += self.limit_clause(row_limit=row_limit)
        return sql

    def fingerprint_sql(self, task: TableTask) -> str | None:
        """A query returning one row (row_count, fingerprint) as text, or None when this table
        can't be fingerprinted (it is then copied on every run)."""
        return None

    def change_marker(self, obj: SourceObject):
        """A cheap metadata value that changes whenever the data does (Snowflake LAST_ALTERED
        on base tables), or None."""
        return obj.last_altered

    def arrow_casts(self, task: TableTask, schema: pa.Schema) -> dict[int, pa.DataType]:
        """Client-side conversions {column index: Arrow type}, for types the driver returns in a
        lossy or awkward form."""
        return {}

    # ---- incremental loads ------------------------------------------------------------------

    def primary_keys(self, conn, catalog: str | None, db_schemas: list[str]) -> dict[tuple[str, str], list[str]]:
        """(db_schema, table) -> primary-key column names in key order, from the driver's ADBC
        GetObjects metadata (no source-specific dictionary queries)."""
        keys: dict[tuple[str, str], list[str]] = {}
        for db_schema in db_schemas:
            reader = conn.adbc_get_objects(depth="all", catalog_filter=catalog, db_schema_filter=db_schema)
            for cat in reader.read_all().to_pylist():
                for schema in cat.get("catalog_db_schemas") or []:
                    for table in schema.get("db_schema_tables") or []:
                        for constraint in table.get("table_constraints") or []:
                            if constraint.get("constraint_type") == "PRIMARY KEY":
                                keys[(schema["db_schema_name"], table["table_name"])] = \
                                    list(constraint["constraint_column_names"])
        return keys

    #: True when the source itself can name the rows changed since a point (Oracle ORA_ROWSCN);
    #: other sources need refresh.watermark_column for incremental loads.
    tracks_row_changes: bool = False

    def change_token(self) -> str | None:
        """Where this run's reads stand (e.g. the snapshot SCN); None if the source can't tell
        which rows changed, so incremental tables fall back to whole-table copies."""
        return None

    def watermark_sql(self, task: TableTask) -> str:
        """One row, one text value: the highest task.watermark_column value now (NULL when empty)."""
        col = self.quote(name=task.watermark_column)
        return f"SELECT CAST(MAX({col}) AS VARCHAR(64)) FROM {self.table_ref(obj=task.source)}"

    def watermark_filter(self, task: TableTask, previous: str, lookback_minutes: int) -> str:
        """Rows stamped after `previous` minus the lookback (a superset is fine: rows are merged)."""
        col = self.quote(name=task.watermark_column)
        return (f"{col} > CAST({quote_literal(value=previous)} AS TIMESTAMP) "
                f"- INTERVAL '{int(lookback_minutes)}' MINUTE")

    def count_sql(self, task: TableTask) -> str:
        return f"SELECT COUNT(*) FROM {self.table_ref(obj=task.source)}"

    def incremental_filter(self, previous_token: str) -> str | None:
        """A WHERE condition selecting the rows changed since `previous_token` (a superset is fine:
        rows are merged on the key)."""
        return None

    def time_zone_warning(self, conn, time_zone: str) -> str | None:
        """A warning when views.source_time_zone doesn't match the source's clock (sources that can tell)."""
        return None

    def error_hint(self, error: str) -> str | None:
        """A short explanation to append to a table failure, for errors with a known cause."""
        return None

    @staticmethod
    def fetch_all(conn, sql: str) -> list[tuple]:
        with conn.cursor() as cur:
            cur.execute(operation=sql)
            return cur.fetchall()
