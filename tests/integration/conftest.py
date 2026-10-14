"""Integration fixtures: a real GizmoSQL target plus Oracle and PostgreSQL sources.

These tests gate releases, so an unreachable server is a FAILURE, never a skip: a dead
service container must not turn into a green job with zero coverage.
"""

from __future__ import annotations

import io
import os
import time
import uuid

import pytest

from adbc_replicate.config import parse_config
from adbc_replicate.engine import Replicator, RunOptions
from adbc_replicate.events import Reporter

GIZMOSQL_URI = os.getenv("ADBC_REPLICATE_TEST_GIZMOSQL_URI", "gizmosql://127.0.0.1:31377?transport=tcp")
GIZMOSQL_USERNAME = os.getenv("ADBC_REPLICATE_TEST_GIZMOSQL_USERNAME", "gizmosql")
GIZMOSQL_PASSWORD = os.getenv("ADBC_REPLICATE_TEST_GIZMOSQL_PASSWORD", "gizmosql")
ORACLE_URI = os.getenv("ADBC_REPLICATE_TEST_ORACLE_URI", "oracle://127.0.0.1:15211/FREEPDB1")
ORACLE_USERNAME = os.getenv("ADBC_REPLICATE_TEST_ORACLE_USERNAME", "replicate")
ORACLE_PASSWORD = os.getenv("ADBC_REPLICATE_TEST_ORACLE_PASSWORD", "replicate")
ORACLE_SYSTEM_PASSWORD = os.getenv("ADBC_REPLICATE_TEST_ORACLE_SYSTEM_PASSWORD", "oracle")
POSTGRES_URI = os.getenv("ADBC_REPLICATE_TEST_POSTGRES_URI", "postgresql://127.0.0.1:15432/source")
POSTGRES_USERNAME = os.getenv("ADBC_REPLICATE_TEST_POSTGRES_USERNAME", "postgres")
POSTGRES_PASSWORD = os.getenv("ADBC_REPLICATE_TEST_POSTGRES_PASSWORD", "postgres")
WAIT_SECONDS = int(os.getenv("ADBC_REPLICATE_TEST_WAIT_SECONDS", "180"))



def _wait(name: str, connect):
    deadline = time.monotonic() + WAIT_SECONDS
    last = None
    while time.monotonic() < deadline:
        try:
            return connect()
        except Exception as e:
            last = e
            time.sleep(3)
    pytest.fail(f"{name} is unreachable after {WAIT_SECONDS}s (start it with `docker compose up -d --wait`): {last}",
                pytrace=False)


def gizmosql_connect():
    import adbc_driver_gizmosql.dbapi as gizmosql
    return gizmosql.connect(uri=GIZMOSQL_URI, username=GIZMOSQL_USERNAME, password=GIZMOSQL_PASSWORD)


def oracle_connect(username=ORACLE_USERNAME, password=ORACLE_PASSWORD, autocommit=True):
    import adbc_driver_oracle.dbapi as oracle
    return oracle.connect(uri=ORACLE_URI, db_kwargs={"username": username, "password": password},
                          autocommit=autocommit)


def postgres_connect(autocommit=True):
    import adbc_driver_postgresql.dbapi as postgresql

    from adbc_replicate.sources.postgresql import _with_credentials
    return postgresql.connect(uri=_with_credentials(uri=POSTGRES_URI, username=POSTGRES_USERNAME,
                                                    password=POSTGRES_PASSWORD), autocommit=autocommit)


def execute(conn, statements):
    with conn.cursor() as cur:
        for sql in statements:
            cur.execute(operation=sql)


def query(conn, sql):
    with conn.cursor() as cur:
        cur.execute(operation=sql)
        return cur.fetchall()


def _drop_oracle(kind: str, name: str) -> str:
    return f"BEGIN EXECUTE IMMEDIATE 'DROP {kind} {name}{' CASCADE CONSTRAINTS' if kind == 'TABLE' else ''}'; " \
           "EXCEPTION WHEN OTHERS THEN NULL; END;"


@pytest.fixture(scope="session")
def gizmosql():
    conn = _wait(name="GizmoSQL", connect=lambda: _probe(connect=gizmosql_connect, sql="SELECT 1"))
    yield conn
    conn.close()


def _probe(connect, sql):
    conn = connect()
    query(conn=conn, sql=sql)
    return conn


@pytest.fixture(scope="session")
def oracle():
    """Seed REPLICATE (the app user the replicator reads as) and a SALES schema it can read."""
    system = _wait(name="Oracle", connect=lambda: _probe(
        connect=lambda: oracle_connect(username="system", password=ORACLE_SYSTEM_PASSWORD), sql="SELECT 1 FROM dual"))
    execute(conn=system, statements=[
        "BEGIN EXECUTE IMMEDIATE 'DROP USER sales CASCADE'; EXCEPTION WHEN OTHERS THEN NULL; END;",
        "CREATE USER sales IDENTIFIED BY sales QUOTA UNLIMITED ON users",
        "CREATE TABLE sales.orders (id NUMBER(10) PRIMARY KEY, emp_id NUMBER, amount NUMBER(12,2), "
        "ordered_at TIMESTAMP(6))",
        "INSERT INTO sales.orders SELECT LEVEL, MOD(LEVEL, 2000) + 1, ROUND(LEVEL * 1.37, 2), "
        "TIMESTAMP '2026-01-01 00:00:00' + NUMTODSINTERVAL(LEVEL, 'MINUTE') FROM dual CONNECT BY LEVEL <= 3000",
        "COMMIT",
        f"GRANT SELECT ON sales.orders TO {ORACLE_USERNAME}",
        f"GRANT FLASHBACK ON sales.orders TO {ORACLE_USERNAME}",
    ])
    system.close()
    # consistency: snapshot reads an exact SCN through DBMS_FLASHBACK (see README); only SYS can grant it
    import adbc_driver_oracle.dbapi as oracle_dbapi
    sys_conn = oracle_dbapi.connect(uri=ORACLE_URI, autocommit=True, db_kwargs={
        "username": "sys", "password": ORACLE_SYSTEM_PASSWORD, "adbc.oracle.mode": "sysdba"})
    execute(conn=sys_conn, statements=[f"GRANT EXECUTE ON SYS.DBMS_FLASHBACK TO {ORACLE_USERNAME}"])
    sys_conn.close()
    conn = oracle_connect()
    drops = [_drop_oracle(kind="VIEW", name=v) for v in
             ("V_EMP", "V_EMP_DEPT", "V_TODAY", "V_TOP10", "V_COLS", "V_ON_V", "V_SALES", "V_TREE")]
    drops += [_drop_oracle(kind="TABLE", name=t) for t in ("EMP", "DEPT", "BIG", "EMP_BAK", "PERF", "FBT")]
    execute(conn=conn, statements=[*drops,
        "CREATE TABLE dept (id NUMBER(10) PRIMARY KEY, name VARCHAR2(50))",
        "INSERT INTO dept SELECT LEVEL, 'Dept ' || LEVEL FROM dual CONNECT BY LEVEL <= 9",
        "CREATE TABLE emp (id NUMBER, dept_id NUMBER(10), name VARCHAR2(100), ssn VARCHAR2(11), "
        "salary NUMBER(12,2), hired DATE, notes CLOB, photo BLOB, updated TIMESTAMP(6), "
        "tz TIMESTAMP(6) WITH TIME ZONE)",
        # dept_id 10 doesn't exist (outer-join rows), every 7th employee has no department
        "INSERT INTO emp SELECT LEVEL, CASE WHEN MOD(LEVEL, 7) = 0 THEN NULL ELSE MOD(LEVEL, 10) + 1 END, "
        "'Employee ' || LEVEL, '123-45-' || LPAD(MOD(LEVEL, 10000), 4, '0'), ROUND(LEVEL * 10.01, 2), "
        "DATE '2020-01-01' + MOD(LEVEL, 2000), TO_CLOB('notes for ' || LEVEL), "
        "HEXTORAW('DEADBEEF'), TIMESTAMP '2026-01-01 12:34:56.123456', "
        "TIMESTAMP '2026-01-01 12:00:00 -05:00' FROM dual CONNECT BY LEVEL <= 2000",
        "CREATE TABLE emp_bak (id NUMBER)",
        "CREATE TABLE big (id NUMBER(10), payload VARCHAR2(200))",
        "INSERT INTO big SELECT LEVEL, RPAD('x', 150, 'y') || LEVEL FROM dual CONNECT BY LEVEL <= 100000",
        "COMMIT",
        "CREATE VIEW v_emp_dept AS SELECT e.id, e.name, d.name AS dept_name FROM emp e, dept d "
        "WHERE e.dept_id = d.id(+)",
        "CREATE VIEW v_today AS SELECT 1 AS one FROM DUAL",
        "CREATE VIEW v_top10 AS SELECT id, name FROM emp WHERE ROWNUM <= 10",
        "CREATE VIEW v_cols (emp_id, emp_name) AS SELECT id, name FROM emp",
        "CREATE VIEW v_on_v AS SELECT dept_name, COUNT(*) AS n FROM v_emp_dept GROUP BY dept_name",
        "CREATE VIEW v_sales AS SELECT o.id, o.amount, e.name FROM sales.orders o JOIN emp e ON e.id = o.emp_id",
        "CREATE VIEW v_tree AS SELECT LEVEL AS lvl FROM dual CONNECT BY LEVEL <= 3",
    ])
    # Oracle can't flashback-read a table in the first ~2 s after it was created (ORA-01466).
    deadline = time.monotonic() + 30
    while True:
        scn = query(conn=conn, sql="SELECT TO_CHAR(DBMS_FLASHBACK.GET_SYSTEM_CHANGE_NUMBER) FROM dual")[0][0]
        try:
            for table in ("emp", "dept", "big", "sales.orders"):
                query(conn=conn, sql=f"SELECT COUNT(*) FROM {table} AS OF SCN {scn}")
            break
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def postgres():
    conn = _wait(name="PostgreSQL", connect=lambda: _probe(connect=postgres_connect, sql="SELECT 1"))
    execute(conn=conn, statements=[
        "DROP SCHEMA IF EXISTS hr CASCADE",
        "DROP SCHEMA IF EXISTS sales CASCADE",
        "CREATE SCHEMA hr",
        "CREATE SCHEMA sales",
        "CREATE TABLE hr.dept (id int PRIMARY KEY, name text)",
        "INSERT INTO hr.dept SELECT i, 'Dept ' || i FROM generate_series(1, 9) i",
        "CREATE TABLE hr.emp (id bigint PRIMARY KEY, dept_id int, name text, ssn text, salary numeric(12,2), "
        "bonus numeric, rating real, hired date, updated timestamptz, profile jsonb, tags text[], badge uuid, "
        "active boolean, photo bytea)",
        "INSERT INTO hr.emp SELECT i, CASE WHEN i % 7 = 0 THEN NULL ELSE i % 10 + 1 END, 'Employee ' || i, "
        "'123-45-' || lpad((i % 10000)::text, 4, '0'), round(i * 10.01, 2), i / 3.0, i * 0.5, "
        "DATE '2020-01-01' + i % 2000, TIMESTAMPTZ '2026-01-01 12:00:00+00' + i * INTERVAL '1 minute', "
        "jsonb_build_object('level', i % 5, 'name', 'e' || i), ARRAY['t' || (i % 3), 'x'], "
        "md5(i::text)::uuid, i % 2 = 0, '\\xdeadbeef'::bytea FROM generate_series(1, 2000) i",
        "CREATE TABLE sales.orders (id bigint, emp_id bigint, amount numeric(14,2), ordered_on date) "
        "PARTITION BY RANGE (ordered_on)",
        "CREATE TABLE sales.orders_2025 PARTITION OF sales.orders FOR VALUES FROM ('2025-01-01') TO ('2026-01-01')",
        "CREATE TABLE sales.orders_2026 PARTITION OF sales.orders FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')",
        "INSERT INTO sales.orders SELECT i, i % 2000 + 1, round(i * 1.37, 2), DATE '2025-07-01' + i % 300 "
        "FROM generate_series(1, 3000) i",
        "CREATE MATERIALIZED VIEW hr.dept_counts AS SELECT dept_id, count(*) AS n FROM hr.emp GROUP BY dept_id",
        "CREATE VIEW hr.v_emp AS SELECT e.id, e.name, d.name AS dept FROM hr.emp e LEFT JOIN hr.dept d ON d.id = e.dept_id",
        "CREATE VIEW hr.v_rich AS WITH x AS (SELECT * FROM hr.emp WHERE salary > 5000) SELECT id, name FROM x",
        "CREATE VIEW hr.v_orders AS SELECT o.id, o.amount, e.name FROM sales.orders o JOIN hr.emp e ON e.id = o.emp_id",
    ])
    yield conn
    conn.close()


@pytest.fixture
def prefix():
    """A unique target db_schema prefix per test, so tests share one GizmoSQL database safely."""
    return "t" + uuid.uuid4().hex[:8]


def gizmosql_target(prefix: str) -> dict:
    return {"type": "gizmosql", "uri": GIZMOSQL_URI, "username": GIZMOSQL_USERNAME, "password": GIZMOSQL_PASSWORD}


def oracle_source(**extra) -> dict:
    return {"type": "oracle", "uri": ORACLE_URI, "username": ORACLE_USERNAME, "password": ORACLE_PASSWORD, **extra}


def postgres_source(**extra) -> dict:
    return {"type": "postgresql", "uri": POSTGRES_URI, "username": POSTGRES_USERNAME,
            "password": POSTGRES_PASSWORD, **extra}


def make_config(prefix: str, source: dict, objects: list, **extra):
    data = {"version": 1, "name": f"it-{prefix}", "source": source, "target": gizmosql_target(prefix=prefix),
            "objects": objects, "state": {"db_schema": f"{prefix}_state"}}
    data.update(extra)
    return parse_config(data=data)


def run(config, **options):
    out = io.StringIO()
    summary = Replicator(config=config, reporter=Reporter(fmt="text", stream=out),
                         options=RunOptions(**options)).run()
    return summary, out.getvalue()
