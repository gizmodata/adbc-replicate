"""Oracle (adbc-driver-oracle) → GizmoSQL, against real servers."""

import hashlib
import json
import time
from decimal import Decimal

import pytest
import yaml
from click.testing import CliRunner
from conftest import make_config, oracle_connect, oracle_source, query, run

from adbc_replicate.cli import main
from adbc_replicate.config import parse_config
from adbc_replicate.engine import EXIT_CONFIG, EXIT_OK, EXIT_VIEW_ERRORS
from adbc_replicate.sources import make_source

pytestmark = pytest.mark.integration

SALT = "pepper"


def objects(prefix, **replicate_rule):
    return [
        {"db_schemas": ["REPLICATE"], "exclude_tables": ["*_BAK"], "exclude_views": ["V_TREE"],
         "target_db_schema": f"{prefix}_{{db_schema}}", **replicate_rule},
        {"db_schemas": ["SALES"], "views": [], "target_db_schema": f"{prefix}_{{db_schema}}"},
    ]


COLUMNS = [{"tables": ["REPLICATE.EMP"], "exclude": ["NOTES"],
            "mask": {"SSN": {"strategy": "hash", "salt": SALT}}}]


def test_full_replication_matches_source(oracle, gizmosql, prefix):
    config = make_config(prefix=prefix, source=oracle_source(consistency="serializable"),
                         objects=objects(prefix=prefix), columns=COLUMNS)
    summary, out = run(config=config)
    assert summary.exit_code == EXIT_OK, out
    assert summary.stats["tables_copied"] == 4 and summary.stats["views_created"] == 6, out
    s, g = f"{prefix}_replicate", f"{prefix}_sales"

    for table, source_table in ((f"{s}.emp", "emp"), (f"{s}.dept", "dept"), (f"{s}.big", "big"),
                                (f"{g}.orders", "sales.orders")):
        assert query(conn=gizmosql, sql=f"SELECT count(*) FROM {table}")[0][0] == \
            int(query(conn=oracle, sql=f"SELECT COUNT(*) FROM {source_table}")[0][0])
    assert query(conn=gizmosql, sql=f"SELECT count(*) FROM duckdb_tables() WHERE schema_name = '{s}' "
                                    "AND table_name = 'emp_bak'")[0][0] == 0

    # types: lossless NUMBER handling, LOBs, timestamps
    types = {r[0]: r[1] for r in query(conn=gizmosql, sql=f"DESCRIBE {s}.emp")}
    assert types["id"] == "DECIMAL(38,10)" and types["salary"] == "DECIMAL(12,2)"
    assert types["hired"] == "TIMESTAMP" and types["updated"] == "TIMESTAMP" and types["photo"] == "BLOB"
    assert types["tz"] == "TIMESTAMP WITH TIME ZONE"
    assert "notes" not in types
    oracle_sum = query(conn=oracle, sql="SELECT TO_CHAR(SUM(salary)) FROM emp")[0][0]
    assert Decimal(query(conn=gizmosql, sql=f"SELECT sum(salary) FROM {s}.emp")[0][0]) == Decimal(oracle_sum)
    assert query(conn=gizmosql, sql=f"SELECT photo FROM {s}.emp WHERE id = 1")[0][0] == bytes.fromhex("DEADBEEF")

    # the SSN was hashed inside Oracle: the raw value never crossed the network
    raw = query(conn=oracle, sql="SELECT ssn FROM emp WHERE id = 42")[0][0]
    masked = query(conn=gizmosql, sql=f"SELECT ssn FROM {s}.emp WHERE id = 42")[0][0]
    assert masked == hashlib.sha256((SALT + raw).encode()).hexdigest()
    assert query(conn=gizmosql, sql=f"SELECT count(*) FROM {s}.emp WHERE ssn LIKE '123-%'")[0][0] == 0

    # views: (+) outer join, DUAL, ROWNUM, column lists, views on views, cross-schema
    def oracle_rows(sql):
        return sorted(tuple("<NULL>" if v is None else str(v) for v in r) for r in query(conn=oracle, sql=sql))

    def target_rows(sql):
        return sorted(tuple("<NULL>" if v is None else str(v) for v in r) for r in query(conn=gizmosql, sql=sql))

    assert target_rows(f"SELECT dept_name, n FROM {s}.v_on_v") == \
        oracle_rows("SELECT dept_name, TO_CHAR(n) FROM v_on_v")
    assert query(conn=gizmosql, sql=f"SELECT count(*) FROM {s}.v_emp_dept WHERE dept_name IS NULL")[0][0] == \
        int(query(conn=oracle, sql="SELECT COUNT(*) FROM v_emp_dept WHERE dept_name IS NULL")[0][0])
    assert query(conn=gizmosql, sql=f"SELECT one FROM {s}.v_today")[0][0] == 1
    assert query(conn=gizmosql, sql=f"SELECT count(*) FROM {s}.v_top10")[0][0] == 10
    assert [r[0] for r in query(conn=gizmosql, sql=f"DESCRIBE {s}.v_cols")] == ["emp_id", "emp_name"]
    assert query(conn=gizmosql, sql=f"SELECT count(*) FROM {s}.v_sales")[0][0] == \
        int(query(conn=oracle, sql="SELECT COUNT(*) FROM v_sales")[0][0])

    # run log + state in the target
    log = query(conn=gizmosql, sql=f"SELECT status, tables_copied, views_created, snapshot FROM {prefix}_state.run_log")
    assert log == [("completed", 4, 6, "serializable transaction")]


def test_unchanged_tables_are_skipped_and_changes_copied(oracle, gizmosql, prefix):
    config = make_config(prefix=prefix, source=oracle_source(), objects=objects(prefix=prefix), columns=COLUMNS)
    run(config=config)
    summary, out = run(config=config)
    assert summary.stats["tables_copied"] == 0 and summary.stats["tables_skipped"] == 4, out
    assert summary.stats["views_created"] == 0 and summary.stats["views_skipped"] == 6, out

    with oracle.cursor() as cur:
        cur.execute(operation="UPDATE dept SET name = 'Renamed' WHERE id = 3")
    try:
        summary, out = run(config=config)
        assert summary.stats["tables_copied"] == 1 and "REPLICATE.DEPT" in out and "content changed" in out, out
        assert query(conn=gizmosql, sql=f"SELECT name FROM {prefix}_replicate.dept WHERE id = 3")[0][0] == "Renamed"
    finally:
        with oracle.cursor() as cur:
            cur.execute(operation="UPDATE dept SET name = 'Dept 3' WHERE id = 3")


def test_untranslatable_view_reported_then_materialized(oracle, gizmosql, prefix):
    rule = [{"db_schemas": ["REPLICATE"], "tables": [], "views": ["V_TREE"], "target_db_schema": f"{prefix}_r"}]
    summary, out = run(config=make_config(prefix=prefix, source=oracle_source(), objects=rule))
    assert summary.exit_code == EXIT_VIEW_ERRORS, out
    assert "REPLICATE.V_TREE" in summary.view_failures
    summary, out = run(config=make_config(prefix=prefix, source=oracle_source(), objects=rule,
                                          views={"on_failure": "materialize"}))
    assert summary.exit_code == EXIT_OK, out
    assert sorted(r[0] for r in query(conn=gizmosql, sql=f"SELECT lvl FROM {prefix}_r.v_tree")) == [1, 2, 3]


def test_parallel_snapshot_run(oracle, gizmosql, prefix):
    config = make_config(prefix=prefix, source=oracle_source(consistency="snapshot"),
                         objects=objects(prefix=prefix), columns=COLUMNS, parallelism=4)
    summary, out = run(config=config)
    assert summary.exit_code == EXIT_OK, out
    assert summary.stats["snapshot"].startswith("SCN "), out
    assert summary.stats["rows_copied"] == 2000 + 9 + 100000 + 3000


def test_serializable_transaction_hides_later_commits(oracle):
    source = make_source(config=parse_config(data={
        "version": 1, "source": oracle_source(), "target": {"type": "duckdb", "path": "/tmp/unused.duckdb"},
        "objects": [{"db_schemas": ["REPLICATE"]}]}).source)
    conn = source.connect()
    try:
        source.begin_serializable(conn=conn)
        before = source.fetch_all(conn=conn, sql="SELECT TO_CHAR(COUNT(*)) FROM sales.orders")[0][0]
        writer = oracle_connect(username="system", password="oracle")
        try:
            with writer.cursor() as cur:
                cur.execute(operation="INSERT INTO sales.orders VALUES (999001, 1, 1, SYSTIMESTAMP)")
            assert source.fetch_all(conn=conn, sql="SELECT TO_CHAR(COUNT(*)) FROM sales.orders")[0][0] == before
        finally:
            with writer.cursor() as cur:
                cur.execute(operation="DELETE FROM sales.orders WHERE id = 999001")
            writer.close()
    finally:
        conn.close()


def test_snapshot_workers_share_one_scn(oracle):
    source = make_source(config=parse_config(data={
        "version": 1, "source": oracle_source(consistency="snapshot"),
        "target": {"type": "duckdb", "path": "/tmp/unused.duckdb"}, "objects": [{"db_schemas": ["REPLICATE"]}]}).source)
    conn = source.connect()
    try:
        source.begin_snapshot(conn=conn)
        sql = "SELECT TO_CHAR(COUNT(*)) FROM \"SALES\".\"ORDERS\" AS OF SCN " + source.snapshot.token
        before = source.fetch_all(conn=conn, sql=sql)[0][0]
        writer = oracle_connect(username="system", password="oracle")
        try:
            with writer.cursor() as cur:
                cur.execute(operation="INSERT INTO sales.orders VALUES (999002, 1, 1, SYSTIMESTAMP)")
            time.sleep(1)
            worker = source.worker_connection(conn=conn)
            try:
                assert source.fetch_all(conn=worker, sql=sql)[0][0] == before
            finally:
                worker.close()
        finally:
            with writer.cursor() as cur:
                cur.execute(operation="DELETE FROM sales.orders WHERE id = 999002")
            writer.close()
    finally:
        conn.close()


def test_row_limit_samples_every_table(oracle, gizmosql, prefix):
    config = make_config(prefix=prefix, source=oracle_source(), objects=objects(prefix=prefix), columns=COLUMNS)
    summary, out = run(config=config, row_limit=5, skip_views=True)
    assert summary.exit_code == EXIT_OK, out
    assert query(conn=gizmosql, sql=f"SELECT count(*) FROM {prefix}_replicate.big")[0][0] == 5
    summary, out = run(config=config, skip_views=True)
    assert query(conn=gizmosql, sql=f"SELECT count(*) FROM {prefix}_replicate.big")[0][0] == 100000, out


def test_cli_typo_in_mask_rule_fails_before_copying(oracle, gizmosql, prefix, tmp_path):
    config = make_config(prefix=prefix, source=oracle_source(), objects=objects(prefix=prefix),
                         columns=[{"tables": ["REPLICATE.EMP"], "mask": {"SNN": "hash"}}])
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json", exclude_none=True)))
    result = CliRunner().invoke(main, ["run", "-c", str(path), "--log-format", "json"])
    assert result.exit_code == EXIT_CONFIG, result.output
    assert "SNN" in result.output
    assert query(conn=gizmosql, sql=f"SELECT count(*) FROM duckdb_tables() WHERE schema_name = '{prefix}_replicate'")[0][0] == 0


def test_cli_plan_and_test_connection(oracle, gizmosql, prefix, tmp_path):
    config = make_config(prefix=prefix, source=oracle_source(), objects=objects(prefix=prefix), columns=COLUMNS)
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json", exclude_none=True)))
    runner = CliRunner()
    result = runner.invoke(main, ["test-connection", "-c", str(path)])
    assert result.exit_code == 0 and result.output.count("✅") == 2, result.output
    result = runner.invoke(main, ["plan", "-c", str(path), "--format", "json"])
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert {v["source"]: v["status"] for v in plan["views"]} == {
        f"REPLICATE.{v}": "translates" for v in ("V_COLS", "V_EMP_DEPT", "V_ON_V", "V_SALES", "V_TODAY", "V_TOP10")}
    emp = next(t for t in plan["tables"] if t["source"] == "REPLICATE.EMP")
    assert emp["excluded_columns"] == ["NOTES"] and emp["masked_columns"] == {"SSN": "hash"}
