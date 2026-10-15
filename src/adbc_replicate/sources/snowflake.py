"""Snowflake source, through adbc-driver-snowflake.

A Snowflake database is a catalog; one connection can read any catalog the role can see, so
objects[].catalog may name another database. Change detection uses LAST_ALTERED for base
tables (it moves on every DML) and HASH_AGG for everything else.
"""

from __future__ import annotations

import datetime

from ..config import SnowflakeSource as SnowflakeConfig
from ..models import MATERIALIZED_VIEW, TABLE, VIEW, Column, SourceObject, TableTask
from ..planner import PlanError
from .base import Snapshot, Source, in_list, quote_literal


def _to_utc(ts: datetime.datetime | None) -> datetime.datetime | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.UTC)
    return ts.astimezone(datetime.UTC)


class SnowflakeSource(Source):
    dialect = "snowflake"
    cross_catalog = True
    config: SnowflakeConfig

    def connect(self):
        import adbc_driver_snowflake.dbapi as snowflake

        return snowflake.connect(uri=self.config.uri, db_kwargs=dict(self.config.options), autocommit=True)

    def prepare_cursor(self, cursor) -> None:
        # The driver's defaults (200 queued batches, 10 concurrent chunk downloads) peak at ~1 GB
        # on wide tables while the target is the slow side; 4 and 2 give the same throughput.
        cursor.adbc_statement.set_options(**{
            "adbc.rpc.result_queue_size": str(self.config.result_queue_size),
            "adbc.snowflake.rpc.prefetch_concurrency": str(self.config.prefetch_concurrency)})

    def begin_snapshot(self, conn) -> Snapshot:
        ts = self.fetch_all(conn=conn, sql=(
            "SELECT TO_VARCHAR(CURRENT_TIMESTAMP(), 'YYYY-MM-DD HH24:MI:SS.FF9 TZHTZM')"))[0][0]
        self.snapshot = Snapshot(token=ts, description=f"time travel AT {ts}")
        return self.snapshot

    # ---- discovery ------------------------------------------------------------------------

    def current_catalog(self, conn) -> str | None:
        return self.fetch_all(conn=conn, sql="SELECT CURRENT_DATABASE()")[0][0]

    def check_catalog(self, conn, catalog: str | None) -> None:
        super().check_catalog(conn=conn, catalog=catalog)
        if self.config.consistency != "snapshot":
            return
        name = catalog or self.current_catalog(conn=conn)
        rows = self.fetch_all(conn=conn, sql=(
            f"SELECT type FROM {self._info_schema(catalog=catalog)}.databases "
            f"WHERE database_name = {quote_literal(value=name)}"))
        if rows and rows[0][0] == "IMPORTED DATABASE":
            # Found on SNOWFLAKE_SAMPLE_DATA: every AT(TIMESTAMP => ...) read fails with 000707.
            raise PlanError(f"catalog {name} is a shared (imported) database, which has no time travel, so "
                            "consistency 'snapshot' can't read it; use consistency: none for this source")

    def error_hint(self, error: str) -> str | None:
        if "000707" in error:
            return ("time travel data isn't available: the table is newer than the snapshot, its "
                    "DATA_RETENTION_TIME_IN_DAYS is 0, or it lives in a shared database; use consistency: none")
        return None

    def _info_schema(self, catalog: str | None) -> str:
        return f"{self.quote(name=catalog)}.information_schema" if catalog else "information_schema"

    def list_db_schemas(self, conn, catalog: str | None) -> list[str]:
        return [r[0] for r in self.fetch_all(conn=conn, sql=(
            f"SELECT schema_name FROM {self._info_schema(catalog=catalog)}.schemata "
            "WHERE schema_name <> 'INFORMATION_SCHEMA' ORDER BY schema_name"))]

    def list_objects(self, conn, catalog: str | None, db_schemas: list[str]) -> list[SourceObject]:
        info = self._info_schema(catalog=catalog)
        names = in_list(values=db_schemas)
        objects: dict[tuple[str, str], SourceObject] = {}
        for db_schema, name, table_type, last_altered in self.fetch_all(conn=conn, sql=(
                f"SELECT table_schema, table_name, table_type, last_altered FROM {info}.tables "
                f"WHERE table_schema IN ({names})")):
            kind = {"VIEW": VIEW, "MATERIALIZED VIEW": MATERIALIZED_VIEW}.get(table_type, TABLE)
            objects[(db_schema, name)] = SourceObject(
                catalog=catalog, db_schema=db_schema, name=name, kind=kind,
                # LAST_ALTERED tracks DML only for base tables; for views it tracks DDL.
                last_altered=_to_utc(ts=last_altered) if table_type == "BASE TABLE" else None)
        for db_schema, table, column, data_type, precision, scale in self.fetch_all(conn=conn, sql=(
                f"SELECT table_schema, table_name, column_name, data_type, numeric_precision, numeric_scale "
                f"FROM {info}.columns WHERE table_schema IN ({names}) "
                "ORDER BY table_schema, table_name, ordinal_position")):
            obj = objects.get((db_schema, table))
            if obj is not None:
                obj.columns.append(Column(name=column, data_type=(data_type or "").upper(),
                                          precision=int(precision) if precision is not None else None,
                                          scale=int(scale) if scale is not None else None))
        return sorted(objects.values(), key=lambda o: (o.db_schema, o.name))

    def view_definitions(self, conn, catalog: str | None, views: list[SourceObject]) -> dict[tuple[str, str], str]:
        db_schemas = sorted({v.db_schema for v in views})
        if not db_schemas:
            return {}
        definitions = {(s, n): d for s, n, d in self.fetch_all(conn=conn, sql=(
            f"SELECT table_schema, table_name, view_definition FROM {self._info_schema(catalog=catalog)}.views "
            f"WHERE table_schema IN ({in_list(values=db_schemas)})"))}
        for view in views:
            if not definitions.get((view.db_schema, view.name)):
                # VIEW_DEFINITION is empty when the role doesn't own the view; GET_DDL still works.
                name = ".".join(p for p in (catalog, view.db_schema, view.name) if p)
                rows = self.fetch_all(conn=conn, sql=f"SELECT GET_DDL('VIEW', {quote_literal(value=name)})")
                definitions[(view.db_schema, view.name)] = rows[0][0]
        return definitions

    # ---- query building -------------------------------------------------------------------

    def table_ref(self, obj: SourceObject) -> str:
        ref = super().table_ref(obj=obj)
        if self.snapshot is not None:
            ref += f" AT(TIMESTAMP => {quote_literal(value=self.snapshot.token)}::TIMESTAMP_TZ)"
        return ref

    def text_expr(self, expr: str) -> str:
        return f"TO_VARCHAR({expr})"

    def sha256_hex_expr(self, expr: str) -> str:
        return f"SHA2({expr}, 256)"

    def fingerprint_sql(self, task: TableTask) -> str | None:
        cols = [self.quote(name=c.column.name) for c in task.columns if c.fingerprint]
        if not cols:
            return None
        return (f"SELECT TO_VARCHAR(COUNT(*)), TO_VARCHAR(HASH_AGG({', '.join(cols)})) "
                f"FROM {self.table_ref(obj=task.source)}")

    def watermark_sql(self, task: TableTask) -> str:
        col = self.quote(name=task.watermark_column)
        return (f"SELECT TO_VARCHAR(MAX({col})::TIMESTAMP_NTZ, 'YYYY-MM-DD HH24:MI:SS.FF9') "
                f"FROM {self.table_ref(obj=task.source)}")

    def watermark_filter(self, task: TableTask, previous: str, lookback_minutes: int) -> str:
        col = self.quote(name=task.watermark_column)
        return (f"{col}::TIMESTAMP_NTZ > DATEADD(minute, -{int(lookback_minutes)}, "
                f"{quote_literal(value=previous)}::TIMESTAMP_NTZ)")
