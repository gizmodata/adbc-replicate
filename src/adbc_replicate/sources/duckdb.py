"""DuckDB source (a local database file), through DuckDB's built-in ADBC driver.

Attached databases are catalogs, so objects[].catalog may name any of them.
"""

from __future__ import annotations

import re

from ..config import DuckDBSource as DuckDBConfig
from ..models import TABLE, VIEW, Column, SourceObject, TableTask
from .base import Source, in_list, quote_literal

# Types DuckDB's Arrow export can't carry: their values arrive changed, with no error. Matched as a type
# (alone, or nested: "TIME WITH TIME ZONE[]", "MAP(VARCHAR, BIT)"), never as a STRUCT field name.
_LOSSY_TYPES = [
    (re.compile(r"(?:^|[(\s,])TIME WITH TIME ZONE(?=$|[)\[,])"),
     "TIME WITH TIME ZONE (TIMETZ) arrives as TIME without its UTC offset"),
    (re.compile(r"(?:^|[(\s,])BIT(?=$|[)\[,])"), "BIT arrives as its internal bytes (BLOB), not its bits"),
    (re.compile(r"(?:^|[(\s,])(?:BIGNUM|VARINT)(?=$|[)\[,])"),
     "BIGNUM arrives as its internal bytes (BLOB), not its value"),
]


def lossy_duckdb_type(data_type: str) -> str | None:
    """Why DuckDB's Arrow export changes values of this type (a duckdb_columns() data_type), or None."""
    for pattern, reason in _LOSSY_TYPES:
        if pattern.search(data_type.upper()):
            return reason
    return None


def duckdb_connect(path: str, read_only: bool = False):
    import adbc_driver_duckdb
    import adbc_driver_manager
    import adbc_driver_manager.dbapi

    kwargs = {"path": path} if path and path != ":memory:" else {}
    if read_only:
        kwargs["access_mode"] = "READ_ONLY"
    db = adbc_driver_manager.AdbcDatabase(driver=adbc_driver_duckdb.driver_path(), entrypoint="duckdb_adbc_init",
                                          **kwargs)
    conn = adbc_driver_manager.AdbcConnection(db)
    return adbc_driver_manager.dbapi.Connection(db=db, conn=conn, autocommit=False)


class DuckDBSource(Source):
    dialect = "duckdb"
    cross_catalog = True
    config: DuckDBConfig

    def connect(self):
        return duckdb_connect(path=self.config.path, read_only=self.config.path != ":memory:")

    def begin_serializable(self, conn) -> None:
        pass   # autocommit is off: everything already runs in one snapshot-isolated transaction

    def _catalog(self, conn, catalog: str | None) -> str:
        return catalog or self.current_catalog(conn=conn)

    def current_catalog(self, conn) -> str | None:
        return self.fetch_all(conn=conn, sql="SELECT current_database()")[0][0]

    def list_db_schemas(self, conn, catalog: str | None) -> list[str]:
        db = quote_literal(value=self._catalog(conn=conn, catalog=catalog))
        # DuckDB flags every database's default schema `main` as internal, like information_schema and
        # pg_catalog; it holds user tables, so it is listed.
        return [r[0] for r in self.fetch_all(conn=conn, sql=(
            f"SELECT schema_name FROM duckdb_schemas() WHERE database_name = {db} "
            "AND (NOT internal OR schema_name = 'main') ORDER BY schema_name"))]

    def list_objects(self, conn, catalog: str | None, db_schemas: list[str]) -> list[SourceObject]:
        db = quote_literal(value=self._catalog(conn=conn, catalog=catalog))
        names = in_list(values=db_schemas)
        objects: dict[tuple[str, str], SourceObject] = {}
        for db_schema, name in self.fetch_all(conn=conn, sql=(
                f"SELECT schema_name, table_name FROM duckdb_tables() WHERE database_name = {db} "
                f"AND schema_name IN ({names}) AND NOT temporary")):
            objects[(db_schema, name)] = SourceObject(catalog=catalog, db_schema=db_schema, name=name, kind=TABLE)
        for db_schema, name in self.fetch_all(conn=conn, sql=(
                f"SELECT schema_name, view_name FROM duckdb_views() WHERE database_name = {db} "
                f"AND schema_name IN ({names}) AND NOT internal AND NOT temporary")):
            objects[(db_schema, name)] = SourceObject(catalog=catalog, db_schema=db_schema, name=name, kind=VIEW)
        for db_schema, table, column, data_type, precision, scale in self.fetch_all(conn=conn, sql=(
                "SELECT schema_name, table_name, column_name, data_type, numeric_precision, numeric_scale "
                f"FROM duckdb_columns() WHERE database_name = {db} AND schema_name IN ({names}) "
                "ORDER BY schema_name, table_name, column_index")):
            obj = objects.get((db_schema, table))
            if obj is not None:
                obj.columns.append(Column(name=column, data_type=(data_type or "").upper(),
                                          precision=precision, scale=scale))
        return sorted(objects.values(), key=lambda o: (o.db_schema, o.name))

    def view_definitions(self, conn, catalog: str | None, views: list[SourceObject]) -> dict[tuple[str, str], str]:
        db = quote_literal(value=self._catalog(conn=conn, catalog=catalog))
        db_schemas = sorted({v.db_schema for v in views})
        if not db_schemas:
            return {}
        return {(s, n): d for s, n, d in self.fetch_all(conn=conn, sql=(
            f"SELECT schema_name, view_name, sql FROM duckdb_views() WHERE database_name = {db} "
            f"AND schema_name IN ({in_list(values=db_schemas)})"))}

    def lossy_type(self, column: Column) -> str | None:
        return lossy_duckdb_type(data_type=column.data_type)

    def text_expr(self, expr: str) -> str:
        return f"CAST({expr} AS VARCHAR)"

    def sha256_hex_expr(self, expr: str) -> str:
        return f"sha256({expr})"

    def fingerprint_sql(self, task: TableTask) -> str | None:
        cols = [self.quote(name=c.column.name) for c in task.columns if c.fingerprint]
        if not cols:
            return None
        return (f"SELECT CAST(count(*) AS VARCHAR), CAST(sum(hash({', '.join(cols)})::HUGEINT) AS VARCHAR) "
                f"FROM {self.table_ref(obj=task.source)}")
