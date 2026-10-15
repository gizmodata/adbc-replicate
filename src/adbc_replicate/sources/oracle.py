"""Oracle source, through adbc-driver-oracle (pure Go, no Oracle Client needed).

ADBC terms: an Oracle user/owner is a db_schema; the connection's PDB/service is the one
catalog (Oracle can't query across databases on one connection without links).
"""

from __future__ import annotations

from ..config import OracleSource as OracleConfig
from ..models import MATERIALIZED_VIEW, TABLE, VIEW, Column, SourceObject, TableTask
from .base import Snapshot, Source, in_list

# Types whose values ORA_HASH accepts directly.
_HASHABLE_PREFIXES = ("NUMBER", "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE", "CHAR", "NCHAR", "VARCHAR2",
                      "NVARCHAR2", "DATE", "TIMESTAMP", "INTERVAL", "RAW", "ROWID", "UROWID", "BOOLEAN")
# ORA_HASH input is capped at 4,000 bytes, so per-row hashes are built from groups of column
# hashes (at most ~22 characters each, LOB "length:hash" included) and then hashed again.
_FINGERPRINT_GROUP = 150


# Types that aren't copied; such columns are skipped (and listed in the plan).
_UNSUPPORTED_TYPES = {
    "BFILE": "BFILE: a pointer to a file on the database server, not data",
    # deprecated since Oracle 8i in favour of CLOB/BLOB; Oracle can't use them in expressions
    # (masks, fingerprints), and a table can have only one
    "LONG": "LONG: a deprecated type Oracle can't use in expressions",
    "LONG RAW": "LONG RAW: a deprecated type Oracle can't use in expressions",
}

class OracleSource(Source):
    dialect = "oracle"
    config: OracleConfig

    def connect(self):
        import adbc_driver_oracle.dbapi as oracle

        db_kwargs = dict(self.config.options)
        if self.config.username is not None:
            db_kwargs["username"] = self.config.username
        if self.config.password is not None:
            db_kwargs["password"] = self.config.password
        return oracle.connect(uri=self.config.uri, db_kwargs=db_kwargs, autocommit=False)

    # ---- consistency ----------------------------------------------------------------------

    def begin_serializable(self, conn) -> None:
        conn.adbc_connection.set_options(**{
            "adbc.connection.transaction.isolation_level": "adbc.connection.transaction.isolation.serializable"})
        conn.rollback()   # start the next (serializable) transaction cleanly

    def begin_snapshot(self, conn) -> Snapshot:
        # Exact SCN where the privileges allow, else TIMESTAMP_TO_SCN (≈3 s granularity: still
        # one consistent point in time, just slightly older than "now").
        attempts = [
            ("SELECT TO_CHAR(DBMS_FLASHBACK.GET_SYSTEM_CHANGE_NUMBER) FROM dual", "DBMS_FLASHBACK"),
            ("SELECT TO_CHAR(current_scn) FROM v$database", "V$DATABASE"),
            ("SELECT TO_CHAR(TIMESTAMP_TO_SCN(SYSTIMESTAMP)) FROM dual", "TIMESTAMP_TO_SCN"),
        ]
        errors = []
        for sql, how in attempts:
            try:
                scn = self.fetch_all(conn=conn, sql=sql)[0][0]
                note = how
                if how == "TIMESTAMP_TO_SCN":
                    note += (", up to ~3 s old: a table altered or truncated in those seconds fails with "
                             "ORA-01466; GRANT EXECUTE ON SYS.DBMS_FLASHBACK for an exact SCN")
                self.snapshot = Snapshot(token=str(scn), description=f"SCN {scn} (from {note})")
                return self.snapshot
            except Exception as e:
                errors.append(f"{how}: {e}")
        raise RuntimeError("couldn't read the current SCN: " + "; ".join(errors))

    tracks_row_changes = True

    def change_token(self) -> str | None:
        return self.snapshot.token if self.snapshot is not None else None

    def incremental_filter(self, previous_token: str) -> str | None:
        # ORA_ROWSCN is the commit SCN of a row's last change (per block unless the table has
        # ROWDEPENDENCIES, so unchanged neighbours come too: harmless with a merge). Unlike a
        # timestamp or sequence column it is commit-ordered, also across RAC instances, so a
        # late-committing transaction is never missed. Read AS OF the run's snapshot SCN.
        if self.snapshot is None:
            return None
        return f"ORA_ROWSCN > {int(previous_token)}"

    def time_zone_warning(self, conn, time_zone: str) -> str | None:
        import datetime
        import zoneinfo
        server = self.fetch_all(conn=conn, sql="SELECT TO_CHAR(SYSTIMESTAMP, 'TZH:TZM') FROM dual")[0][0]
        offset = datetime.datetime.now(tz=zoneinfo.ZoneInfo(time_zone)).strftime("%z")
        expected = f"{offset[:3]}:{offset[3:]}"
        if server.strip() != expected:
            return (f"views.source_time_zone {time_zone} is UTC{expected} now, but the Oracle server's clock is "
                    f"UTC{server.strip()}: SYSDATE in translated views would be off")
        return None

    def error_hint(self, error: str) -> str | None:
        if "ORA-01466" in error:
            return ("the table was created, altered or truncated after the snapshot SCN (or less than ~3 s "
                    "before it), so Oracle can't read it AS OF that SCN; run again, or use consistency: "
                    "serializable")
        if "ORA-01555" in error:
            return ("snapshot too old: the run outlasted the database's undo retention; raise UNDO_RETENTION, "
                    "copy fewer tables per run, or raise parallelism")
        if "ORA-01031" in error or "ORA-00942" in error:
            return "the replication user lacks a privilege (SELECT, or FLASHBACK for consistency: snapshot)"
        return None

    # ---- discovery ------------------------------------------------------------------------

    def current_catalog(self, conn) -> str | None:
        return self.fetch_all(conn=conn, sql="SELECT SYS_CONTEXT('USERENV', 'CON_NAME') FROM dual")[0][0]

    def list_db_schemas(self, conn, catalog: str | None) -> list[str]:
        return [r[0] for r in self.fetch_all(conn=conn, sql="SELECT username FROM all_users ORDER BY username")]

    def list_objects(self, conn, catalog: str | None, db_schemas: list[str]) -> list[SourceObject]:
        owners = in_list(values=db_schemas)
        objects: dict[tuple[str, str], SourceObject] = {}
        for owner, name, external in self.fetch_all(conn=conn, sql=(
                "SELECT owner, table_name, external FROM all_tables "
                f"WHERE owner IN ({owners}) AND nested = 'NO' AND secondary = 'N' AND temporary = 'N' "
                "AND dropped = 'NO' AND (iot_type IS NULL OR iot_type = 'IOT')")):
            if external == "YES" and self.config.external_tables == "exclude":
                continue
            objects[(owner, name)] = SourceObject(catalog=catalog, db_schema=owner, name=name, kind=TABLE,
                                                  external=external == "YES")
        for owner, name in self.fetch_all(conn=conn, sql=(
                f"SELECT owner, mview_name FROM all_mviews WHERE owner IN ({owners})")):
            objects[(owner, name)] = SourceObject(catalog=catalog, db_schema=owner, name=name, kind=MATERIALIZED_VIEW)
        for owner, name in self.fetch_all(conn=conn, sql=(
                f"SELECT owner, view_name FROM all_views WHERE owner IN ({owners})")):
            objects[(owner, name)] = SourceObject(catalog=catalog, db_schema=owner, name=name, kind=VIEW)
        for owner, table, column, data_type, precision, scale in self.fetch_all(conn=conn, sql=(
                "SELECT owner, table_name, column_name, data_type, "
                "TO_CHAR(data_precision), TO_CHAR(data_scale) FROM all_tab_columns "
                f"WHERE owner IN ({owners}) ORDER BY owner, table_name, column_id")):
            obj = objects.get((owner, table))
            if obj is not None:
                data_type = (data_type or "").upper()
                obj.columns.append(Column(name=column, data_type=data_type,
                                          precision=int(precision) if precision is not None else None,
                                          scale=int(scale) if scale is not None else None,
                                          unsupported=_UNSUPPORTED_TYPES.get(data_type)))
        return sorted(objects.values(), key=lambda o: (o.db_schema, o.name))

    def view_definitions(self, conn, catalog: str | None, views: list[SourceObject]) -> dict[tuple[str, str], str]:
        owners = sorted({v.db_schema for v in views})
        if not owners:
            return {}
        return {(owner, name): text for owner, name, text in self.fetch_all(conn=conn, sql=(
            f"SELECT owner, view_name, text FROM all_views WHERE owner IN ({in_list(values=owners)})"))}

    # ---- query building -------------------------------------------------------------------

    def table_ref(self, obj: SourceObject) -> str:
        ref = f"{self.quote(name=obj.db_schema)}.{self.quote(name=obj.name)}"
        # External tables read files, which have no SCN (AS OF fails with ORA-08188).
        if self.snapshot is not None and not obj.external:
            ref += f" AS OF SCN {int(self.snapshot.token)}"
        return ref

    def text_expr(self, expr: str) -> str:
        return f"TO_CHAR({expr})"

    def sha256_hex_expr(self, expr: str) -> str:
        return f"LOWER(RAWTOHEX(STANDARD_HASH({expr}, 'SHA256')))"

    def native_expr(self, column: Column) -> str:
        col = self.quote(name=column.name)
        if column.data_type == "NUMBER" and column.precision is None and column.scale is None:
            if self.config.unconstrained_number == "decimal":
                return f"CAST({col} AS NUMBER(38,10))"
            if self.config.unconstrained_number == "string":
                return f"TO_CHAR({col})"
        return col

    def limit_clause(self, row_limit: int) -> str:
        return f" FETCH FIRST {int(row_limit)} ROWS ONLY"

    @staticmethod
    def _column_hash(column: Column, col: str) -> str | None:
        data_type = column.data_type
        if data_type in ("CLOB", "NCLOB"):
            return f"DBMS_LOB.GETLENGTH({col}) || ':' || ORA_HASH(DBMS_LOB.SUBSTR({col}, 1000, 1))"
        if data_type == "BLOB":
            return f"DBMS_LOB.GETLENGTH({col}) || ':' || ORA_HASH(DBMS_LOB.SUBSTR({col}, 2000, 1))"
        if data_type.startswith(_HASHABLE_PREFIXES):
            return f"ORA_HASH({col})"
        return None   # LONG, XMLTYPE, object types, ... -> not fingerprintable

    def fingerprint_sql(self, task: TableTask) -> str | None:
        parts = []
        for c in task.columns:
            if not c.fingerprint:
                continue
            h = self._column_hash(column=c.column, col=self.quote(name=c.column.name))
            if h is None:
                return None
            parts.append(f"NVL(TO_CHAR({h}), '~')")
        if not parts:
            return None

        separator = " || ',' || "

        def row_hash(seed: int) -> str:
            groups = [separator.join(parts[i:i + _FINGERPRINT_GROUP])
                      for i in range(0, len(parts), _FINGERPRINT_GROUP)]
            hashed = [f"ORA_HASH({g}, 4294967295, {seed})" for g in groups]
            if len(hashed) == 1:
                return hashed[0]
            combined = separator.join(f"TO_CHAR({h})" for h in hashed)
            return f"ORA_HASH({combined}, 4294967295, {seed})"

        # Two independently seeded 32-bit row hashes, summed: an order-independent ~64-bit
        # content fingerprint (Oracle has no HASH_AGG).
        return (f"SELECT TO_CHAR(COUNT(*)), "
                f"TO_CHAR(SUM({row_hash(seed=0)})) || ':' || TO_CHAR(SUM({row_hash(seed=1)})) "
                f"FROM {self.table_ref(obj=task.source)}")
