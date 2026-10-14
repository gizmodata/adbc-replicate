"""refresh.mode: incremental against Oracle: rows changed since the last run's snapshot SCN (ORA_ROWSCN),
merged on the primary key (discovered through ADBC GetObjects), with delete detection. Every run ends
with each target table compared to its source table, row for row."""

import time

import pytest
from conftest import (
    ORACLE_SYSTEM_PASSWORD,
    ORACLE_USERNAME,
    execute,
    make_config,
    oracle_connect,
    oracle_source,
    query,
    run,
)

from adbc_replicate.engine import EXIT_OK

pytestmark = pytest.mark.integration

SCHEMA = "INCSRC"
TABLES = {"ORDERS": "id", "ORDER_LINES": "order_id, line_no", "NO_KEY": "id"}


@pytest.fixture(scope="module")
def inc_source(oracle):
    system = oracle_connect(username="system", password=ORACLE_SYSTEM_PASSWORD)
    execute(conn=system, statements=[
        f"BEGIN EXECUTE IMMEDIATE 'DROP USER {SCHEMA} CASCADE'; EXCEPTION WHEN OTHERS THEN NULL; END;",
        f"CREATE USER {SCHEMA} IDENTIFIED BY incsrc QUOTA UNLIMITED ON users",
        f"CREATE TABLE {SCHEMA}.orders (id NUMBER(10) PRIMARY KEY, status VARCHAR2(20), amount NUMBER(12,2))",
        f"INSERT INTO {SCHEMA}.orders SELECT LEVEL, 'open', LEVEL * 1.25 FROM dual CONNECT BY LEVEL <= 5000",
        f"CREATE TABLE {SCHEMA}.order_lines (order_id NUMBER(10), line_no NUMBER(5), qty NUMBER(8), "
        "PRIMARY KEY (order_id, line_no))",
        f"INSERT INTO {SCHEMA}.order_lines SELECT CEIL(LEVEL / 3), MOD(LEVEL, 3) + 1, LEVEL FROM dual "
        "CONNECT BY LEVEL <= 9000",
        f"CREATE TABLE {SCHEMA}.no_key (id NUMBER(10), v VARCHAR2(20))",
        f"INSERT INTO {SCHEMA}.no_key SELECT LEVEL, 'v' || LEVEL FROM dual CONNECT BY LEVEL <= 100",
        "COMMIT",
        *[f"GRANT SELECT ON {SCHEMA}.{t} TO {ORACLE_USERNAME}" for t in TABLES],
        *[f"GRANT FLASHBACK ON {SCHEMA}.{t} TO {ORACLE_USERNAME}" for t in TABLES],
    ])
    yield system
    system.close()


def wait_for_flashback(conn):
    # Oracle can't flashback-read a table in the first seconds after it's created (ORA-01466).
    deadline = time.monotonic() + 30
    while True:
        scn = query(conn=conn, sql="SELECT TO_CHAR(DBMS_FLASHBACK.GET_SYSTEM_CHANGE_NUMBER) FROM dual")[0][0]
        try:
            for table in TABLES:
                query(conn=conn, sql=f"SELECT COUNT(*) FROM {SCHEMA}.{table} AS OF SCN {scn}")
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def config(prefix, **refresh):
    return make_config(prefix=prefix, source=oracle_source(consistency="snapshot"), parallelism=2,
                       objects=[{"db_schemas": [SCHEMA], "target_db_schema": f"{prefix}_inc"}],
                       refresh={"mode": "incremental", **refresh})


def assert_identical(source, gizmosql, prefix):
    for table, order in TABLES.items():
        expected = query(conn=source, sql=f"SELECT * FROM {SCHEMA}.{table} ORDER BY {order}")
        actual = query(conn=gizmosql, sql=f"SELECT * FROM {prefix}_inc.{table.lower()} ORDER BY {order}")
        assert actual == expected, f"{table}: {len(actual)} target rows vs {len(expected)} source rows"


def test_incremental_merges_changes_and_deletes(inc_source, gizmosql, prefix):
    source = oracle_connect()
    wait_for_flashback(conn=source)

    summary, out = run(config=config(prefix=prefix))               # first run: whole tables + change tokens
    assert summary.exit_code == EXIT_OK and summary.stats["tables_copied"] == 3, out
    assert_identical(source=source, gizmosql=gizmosql, prefix=prefix)

    execute(conn=inc_source, statements=[
        f"UPDATE {SCHEMA}.orders SET status = 'shipped', amount = amount + 1 WHERE id IN (10, 2500, 4999)",
        f"INSERT INTO {SCHEMA}.orders VALUES (6001, 'new', 1.00)",
        f"INSERT INTO {SCHEMA}.orders VALUES (6002, 'new', 2.00)",
        f"DELETE FROM {SCHEMA}.orders WHERE id = 77",
        f"UPDATE {SCHEMA}.order_lines SET qty = -1 WHERE order_id = 5 AND line_no = 2",
        f"DELETE FROM {SCHEMA}.order_lines WHERE order_id = 6",
        f"UPDATE {SCHEMA}.no_key SET v = 'changed' WHERE id = 1",
        "COMMIT",
    ])
    summary, out = run(config=config(prefix=prefix))
    assert summary.exit_code == EXIT_OK, out
    assert summary.stats["tables_merged"] == 2 and summary.stats["tables_copied"] == 1, out   # no_key: whole table
    assert "🔄 Merging INCSRC.ORDERS" in out and "key id" in out, out
    assert summary.stats["rows_deleted"] == 1 + 3, out                                        # order 77, order 6's lines
    # only blocks touched since the last run are read, never the whole table
    assert summary.stats["rows_merged"] < 5000 + 9000, out
    assert_identical(source=source, gizmosql=gizmosql, prefix=prefix)

    summary, out = run(config=config(prefix=prefix))               # nothing changed since
    assert summary.exit_code == EXIT_OK and summary.stats["rows_merged"] == 0, out
    assert summary.stats["rows_deleted"] == 0, out
    assert_identical(source=source, gizmosql=gizmosql, prefix=prefix)

    # without delete detection, a deleted source row stays in the target
    execute(conn=inc_source, statements=[f"DELETE FROM {SCHEMA}.orders WHERE id = 78", "COMMIT"])
    summary, out = run(config=config(prefix=prefix, detect_deletes=False))
    assert summary.stats.get("rows_deleted", 0) == 0, out
    assert query(conn=gizmosql, sql=f"SELECT count(*) FROM {prefix}_inc.orders WHERE id = 78")[0][0] == 1
    source.close()


def test_plan_shows_which_tables_are_incremental(inc_source, prefix):
    from adbc_replicate.engine import Replicator, RunOptions
    replicator = Replicator(config=config(prefix=prefix), options=RunOptions())
    conn = replicator.source.connect()
    try:
        plan = replicator.build_plan(source_conn=conn)
    finally:
        conn.close()
    status = {t.source.name: (t.incremental_key, t.incremental_note) for t in plan.tables}
    assert status == {"ORDERS": (["id"], None), "ORDER_LINES": (["order_id", "line_no"], None),
                      "NO_KEY": (None, "no primary key")}
