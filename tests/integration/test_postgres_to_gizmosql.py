"""PostgreSQL (adbc-driver-postgresql) → GizmoSQL, against real servers."""

from decimal import Decimal

import pytest
from conftest import make_config, postgres_connect, postgres_source, query, run

from adbc_replicate.config import parse_config
from adbc_replicate.engine import EXIT_OK, EXIT_VIEW_ERRORS
from adbc_replicate.sources import make_source

pytestmark = pytest.mark.integration


def objects(prefix):
    return [{"db_schemas": ["hr", "sales"], "target_db_schema": f"{prefix}_{{db_schema}}"}]


def test_full_replication_matches_source(postgres, gizmosql, prefix):
    config = make_config(prefix=prefix, source=postgres_source(consistency="serializable"), objects=objects(prefix),
                         columns=[{"tables": ["hr.emp"], "mask": {"ssn": "redact"}}])
    summary, out = run(config=config)
    assert summary.exit_code == EXIT_OK, out
    h, s = f"{prefix}_hr", f"{prefix}_sales"
    # tables, the partitioned table (once, not per partition) and the materialized view
    assert summary.stats["tables_copied"] == 4, out
    for table, source_table in ((f"{h}.emp", "hr.emp"), (f"{h}.dept", "hr.dept"), (f"{s}.orders", "sales.orders"),
                                (f"{h}.dept_counts", "hr.dept_counts")):
        assert query(conn=gizmosql, sql=f"SELECT count(*) FROM {table}")[0][0] == \
            query(conn=postgres, sql=f"SELECT count(*) FROM {source_table}")[0][0]
    tables = {r[0] for r in query(conn=gizmosql, sql=f"SELECT table_name FROM duckdb_tables() WHERE schema_name = '{s}'")}
    assert tables == {"orders"}

    types = {r[0]: r[1] for r in query(conn=gizmosql, sql=f"DESCRIBE {h}.emp")}
    assert types["salary"] == "DECIMAL(12,2)" and types["bonus"] == "DECIMAL(38,10)"
    assert types["badge"] == "VARCHAR" and types["tags"] == "VARCHAR[]" and types["updated"] == "TIMESTAMP WITH TIME ZONE"
    assert types["photo"] == "BLOB"

    pg = query(conn=postgres, sql="SELECT sum(salary)::text, badge::text, profile::text FROM hr.emp "
                                  "WHERE id = 42 GROUP BY badge, profile")[0]
    target = query(conn=gizmosql, sql=f"SELECT (SELECT sum(salary) FROM {h}.emp WHERE id = 42), badge, profile "
                                      f"FROM {h}.emp WHERE id = 42")[0]
    assert Decimal(target[0]) == Decimal(pg[0]) and target[1] == pg[1] and target[2] == pg[2]
    assert Decimal(query(conn=gizmosql, sql=f"SELECT sum(bonus) FROM {h}.emp")[0][0]) == \
        Decimal(query(conn=postgres, sql="SELECT round(sum(round(bonus, 10)), 10)::text FROM hr.emp")[0][0])
    assert query(conn=gizmosql, sql=f"SELECT DISTINCT ssn FROM {h}.emp") == [("****",)]

    # views: LEFT JOIN, CTE, cross-schema
    assert query(conn=gizmosql, sql=f"SELECT count(*) FROM {h}.v_emp WHERE dept IS NULL")[0][0] == \
        query(conn=postgres, sql="SELECT count(*) FROM hr.v_emp WHERE dept IS NULL")[0][0]
    assert query(conn=gizmosql, sql=f"SELECT count(*) FROM {h}.v_rich")[0][0] == \
        query(conn=postgres, sql="SELECT count(*) FROM hr.v_rich")[0][0]
    assert query(conn=gizmosql, sql=f"SELECT count(*) FROM {h}.v_orders")[0][0] == \
        query(conn=postgres, sql="SELECT count(*) FROM hr.v_orders")[0][0]


def test_view_reading_an_excluded_column_fails_instead_of_leaking(postgres, gizmosql, prefix):
    # PostgreSQL stores hr.v_rich's SELECT * as an explicit column list that includes photo.
    config = make_config(prefix=prefix, source=postgres_source(), objects=objects(prefix),
                         columns=[{"tables": ["hr.emp"], "exclude": ["photo"]}])
    summary, out = run(config=config)
    assert summary.exit_code == EXIT_VIEW_ERRORS, out
    assert list(summary.view_failures) == ["hr.v_rich"] and "photo" in summary.view_failures["hr.v_rich"]
    assert "photo" not in [r[0] for r in query(conn=gizmosql, sql=f"DESCRIBE {prefix}_hr.emp")]


def test_second_run_skips_and_detects_changes(postgres, gizmosql, prefix):
    config = make_config(prefix=prefix, source=postgres_source(), objects=objects(prefix))
    run(config=config)
    summary, out = run(config=config)
    assert summary.stats["tables_copied"] == 0 and summary.stats["tables_skipped"] == 4, out
    with postgres.cursor() as cur:
        cur.execute(operation="UPDATE sales.orders SET amount = amount + 1 WHERE id = 7")
    summary, out = run(config=config)
    assert summary.stats["tables_copied"] == 1 and "sales.orders" in out, out


def test_parallel_snapshot_run(postgres, gizmosql, prefix):
    config = make_config(prefix=prefix, source=postgres_source(consistency="snapshot"), objects=objects(prefix),
                         parallelism=4)
    summary, out = run(config=config)
    assert summary.exit_code == EXIT_OK, out
    assert summary.stats["snapshot"].startswith("exported snapshot"), out
    assert summary.stats["tables_copied"] == 4


def _source(consistency):
    return make_source(config=parse_config(data={
        "version": 1, "source": postgres_source(consistency=consistency),
        "target": {"type": "duckdb", "path": "/tmp/unused.duckdb"}, "objects": [{"db_schemas": ["hr"]}]}).source)


def test_exported_snapshot_is_shared_by_workers(postgres):
    source = _source(consistency="snapshot")
    conn = source.connect()
    try:
        source.begin_snapshot(conn=conn)
        before = source.fetch_all(conn=conn, sql="SELECT count(*) FROM hr.dept")[0][0]
        writer = postgres_connect()
        try:
            with writer.cursor() as cur:
                cur.execute(operation="INSERT INTO hr.dept VALUES (99, 'late')")
            worker = source.worker_connection(conn=conn)
            try:
                assert source.fetch_all(conn=worker, sql="SELECT count(*) FROM hr.dept")[0][0] == before
            finally:
                worker.close()
        finally:
            with writer.cursor() as cur:
                cur.execute(operation="DELETE FROM hr.dept WHERE id = 99")
            writer.close()
    finally:
        conn.close()


def test_serializable_transaction_hides_later_commits(postgres):
    source = _source(consistency="serializable")
    conn = source.connect()
    try:
        source.begin_serializable(conn=conn)
        before = source.fetch_all(conn=conn, sql="SELECT count(*) FROM hr.dept")[0][0]
        writer = postgres_connect()
        try:
            with writer.cursor() as cur:
                cur.execute(operation="INSERT INTO hr.dept VALUES (98, 'late')")
            assert source.fetch_all(conn=conn, sql="SELECT count(*) FROM hr.dept")[0][0] == before
        finally:
            with writer.cursor() as cur:
                cur.execute(operation="DELETE FROM hr.dept WHERE id = 98")
            writer.close()
    finally:
        conn.close()
