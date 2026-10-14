"""PostgreSQL source, through adbc-driver-postgresql (libpq + COPY binary).

The connection's database is the one catalog. Consistency uses PostgreSQL's own machinery: a
REPEATABLE READ READ ONLY transaction, and for parallel workers an exported snapshot
(pg_export_snapshot / SET TRANSACTION SNAPSHOT) so every worker sees the same instant.
"""

from __future__ import annotations

from urllib.parse import quote, urlsplit, urlunsplit

import pyarrow as pa

from ..config import PostgresSource as PostgresConfig
from ..models import MATERIALIZED_VIEW, TABLE, VIEW, Column, SourceObject, TableTask
from .base import Snapshot, Source, in_list, quote_literal

_KINDS = {"r": TABLE, "p": TABLE, "f": TABLE, "v": VIEW, "m": MATERIALIZED_VIEW}
_BEGIN = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"


def _with_credentials(uri: str, username: str | None, password: str | None) -> str:
    """libpq takes credentials only in the URI, so inject them (URL-encoded)."""
    if username is None and password is None:
        return uri
    parts = urlsplit(uri)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{parts.port}" if parts.port else "")
    user = quote(username if username is not None else (parts.username or ""), safe="")
    userinfo = user + (f":{quote(password, safe='')}" if password is not None else "")
    return urlunsplit((parts.scheme, f"{userinfo}@{netloc}", parts.path, parts.query, parts.fragment))


class PostgresSource(Source):
    dialect = "postgres"
    config: PostgresConfig

    def connect(self):
        import adbc_driver_postgresql.dbapi as postgresql

        uri = _with_credentials(uri=self.config.uri, username=self.config.username, password=self.config.password)
        # autocommit on: transactions are opened explicitly (BEGIN ...) when a consistency mode
        # asks for one, so the isolation level and snapshot are under our control.
        return postgresql.connect(uri=uri, db_kwargs=dict(self.config.options) or None, autocommit=True)

    # ---- consistency ----------------------------------------------------------------------

    def begin_serializable(self, conn) -> None:
        self._execute(conn=conn, sql=_BEGIN)

    def begin_snapshot(self, conn) -> Snapshot:
        self._execute(conn=conn, sql=_BEGIN)
        snapshot_id = self.fetch_all(conn=conn, sql="SELECT pg_export_snapshot()")[0][0]
        self.snapshot = Snapshot(token=snapshot_id, description=f"exported snapshot {snapshot_id}")
        return self.snapshot

    def join_snapshot(self, conn, snapshot: Snapshot) -> None:
        self._execute(conn=conn, sql=_BEGIN)
        self._execute(conn=conn, sql=f"SET TRANSACTION SNAPSHOT {quote_literal(value=snapshot.token)}")

    @staticmethod
    def _execute(conn, sql: str) -> None:
        with conn.cursor() as cur:
            cur.execute(operation=sql)

    # ---- discovery ------------------------------------------------------------------------

    def current_catalog(self, conn) -> str | None:
        return self.fetch_all(conn=conn, sql="SELECT current_database()")[0][0]

    def list_db_schemas(self, conn, catalog: str | None) -> list[str]:
        return [r[0] for r in self.fetch_all(conn=conn, sql=(
            "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' "
            "AND nspname <> 'information_schema' ORDER BY nspname"))]

    def list_objects(self, conn, catalog: str | None, db_schemas: list[str]) -> list[SourceObject]:
        names = in_list(values=db_schemas)
        objects: dict[tuple[str, str], SourceObject] = {}
        for db_schema, name, relkind in self.fetch_all(conn=conn, sql=(
                "SELECT n.nspname, c.relname, c.relkind::text FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                f"WHERE n.nspname IN ({names}) AND c.relkind IN ('r', 'p', 'f', 'v', 'm') "
                "AND NOT c.relispartition")):
            objects[(db_schema, name)] = SourceObject(catalog=catalog, db_schema=db_schema, name=name,
                                                      kind=_KINDS[relkind])
        for db_schema, table, column, data_type, precision, scale in self.fetch_all(conn=conn, sql=(
                "SELECT n.nspname, c.relname, a.attname, t.typname, "
                "CASE WHEN t.typname = 'numeric' AND a.atttypmod >= 4 THEN ((a.atttypmod - 4) >> 16) & 65535 END, "
                "CASE WHEN t.typname = 'numeric' AND a.atttypmod >= 4 THEN (a.atttypmod - 4) & 65535 END "
                "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_type t ON t.oid = a.atttypid "
                f"WHERE n.nspname IN ({names}) AND c.relkind IN ('r', 'p', 'f', 'v', 'm') "
                "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY n.nspname, c.relname, a.attnum")):
            obj = objects.get((db_schema, table))
            if obj is not None:
                obj.columns.append(Column(name=column, data_type=data_type,
                                          precision=int(precision) if precision is not None else None,
                                          scale=int(scale) if scale is not None else None))
        return sorted(objects.values(), key=lambda o: (o.db_schema, o.name))

    def view_definitions(self, conn, catalog: str | None, views: list[SourceObject]) -> dict[tuple[str, str], str]:
        db_schemas = sorted({v.db_schema for v in views})
        if not db_schemas:
            return {}
        return {(s, n): d for s, n, d in self.fetch_all(conn=conn, sql=(
            "SELECT n.nspname, c.relname, pg_get_viewdef(c.oid, true) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE c.relkind = 'v' AND n.nspname IN ({in_list(values=db_schemas)})"))}

    # ---- query building -------------------------------------------------------------------

    def text_expr(self, expr: str) -> str:
        return f"({expr})::text"

    def sha256_hex_expr(self, expr: str) -> str:
        return f"encode(sha256(convert_to({expr}, 'UTF8')), 'hex')"

    def native_expr(self, column: Column) -> str:
        col = self.quote(name=column.name)
        if column.data_type in ("uuid", "inet", "cidr", "macaddr", "macaddr8", "money", "xml", "tsvector"):
            return f"{col}::text"
        if column.data_type == "numeric" and column.precision is None \
                and self.config.unconstrained_numeric == "decimal":
            # Round to the target scale in the source (as Oracle's CAST to NUMBER(38,10) does), so
            # the strict client-side cast fails only when the integer part overflows.
            return f"round({col}, 10)"
        return col

    def fingerprint_sql(self, task: TableTask) -> str | None:
        cols = [self.quote(name=c.column.name) for c in task.columns if c.fingerprint]
        if not cols:
            return None
        # hashtextextended: a 64-bit hash of the row's text form; summed, it is order-independent.
        return (f"SELECT count(*)::text, sum(hashtextextended(ROW({', '.join(cols)})::text, 0)::numeric)::text "
                f"FROM {self.table_ref(obj=task.source)}")

    def arrow_casts(self, task: TableTask, schema: pa.Schema) -> dict[int, pa.DataType]:
        # The driver returns numeric as text; convert it to a decimal of the declared precision.
        casts: dict[int, pa.DataType] = {}
        for i, selected in enumerate(task.columns):
            column = selected.column
            if column.data_type != "numeric" or (selected.mask is not None and selected.mask.strategy != "null"):
                continue
            if column.precision is not None and column.precision <= 38:
                casts[i] = pa.decimal128(precision=column.precision, scale=column.scale or 0)
            elif column.precision is None:
                mode = self.config.unconstrained_numeric
                if mode == "decimal":
                    casts[i] = pa.decimal128(precision=38, scale=10)
                elif mode == "double":
                    casts[i] = pa.float64()
        return casts
